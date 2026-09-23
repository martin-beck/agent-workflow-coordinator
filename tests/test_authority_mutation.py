# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile tests for the backend-neutral authority effect contract."""

from __future__ import annotations

import unittest

from tools.authority_mutation import (
    AuthorityMutationAmbiguousError,
    AuthorityMutationError,
    BoundAuthorityMutation,
    DurableBoundAuthorityMutation,
)
from tools.authority_neutral_commit import CommitAdmissionBundle
from tools.git_authority_mutation import GitCommitResult, GitMutationAmbiguousError
from tools.sqlite_authority_mutation import SQLiteMutationAmbiguousError


def _admission(backend: str = "git") -> CommitAdmissionBundle:
    return CommitAdmissionBundle(
        backend=backend,
        target="new",
        operation_id="op-1:commit",
        fencing_token="fence-1",  # noqa: S106
        state_revision=1,
        barrier_id="barrier-1",
        artifact_identity="artifact-1",
        manifest_identity="manifest-1",
        selector_identity="selector-1",
        runtime_identity="runtime-1",
    )


class AuthorityMutationTests(unittest.TestCase):
    @staticmethod
    def _result() -> dict[str, object]:
        return {
            "operation_id": "op-1:commit",
            "fencing_token": "fence-1",
            "mutates_authority": True,
        }

    def test_effect_is_single_use_and_identity_bound(self) -> None:
        capability = BoundAuthorityMutation(_admission())
        receipt = capability.execute(
            "git",
            lambda: GitCommitResult(
                operation_id="op-1:commit",
                before_head="a" * 40,
                after_head="b" * 40,
                branch="main",
                fencing_token="fence-1",  # noqa: S106
            ),
        )
        self.assertEqual("git", receipt.backend)
        with self.assertRaisesRegex(AuthorityMutationError, "already consumed"):
            capability.execute("git", self._result)

    def test_rejects_backend_or_result_identity_drift(self) -> None:
        with self.assertRaisesRegex(AuthorityMutationError, "backend identity"):
            BoundAuthorityMutation(_admission()).execute("sqlite", self._result)
        with self.assertRaisesRegex(AuthorityMutationError, "result identity"):
            BoundAuthorityMutation(_admission()).execute(
                "git", lambda: {**self._result(), "fencing_token": "foreign"}
            )

    def test_rejects_noncallable_effect_before_consuming_capability(self) -> None:
        capability = BoundAuthorityMutation(_admission())
        with self.assertRaisesRegex(AuthorityMutationError, "effect is invalid"):
            capability.execute("git", None)  # type: ignore[arg-type]
        capability.execute("git", self._result)

    def test_durable_capability_rejects_invalid_session_revision(self) -> None:
        with self.assertRaisesRegex(AuthorityMutationError, "session revision is invalid"):
            DurableBoundAuthorityMutation(_admission(), object(), session_revision=0)  # type: ignore[arg-type]

    def test_durable_capability_rejects_admission_session_revision_mismatch(self) -> None:
        with self.assertRaisesRegex(AuthorityMutationError, "revision mismatch"):
            DurableBoundAuthorityMutation(_admission(), object(), session_revision=2)  # type: ignore[arg-type]

    def test_ambiguous_effect_consumes_token_and_cannot_retry(self) -> None:
        capability = BoundAuthorityMutation(_admission("sqlite"))

        def ambiguous() -> object:
            raise TimeoutError("commit timeout")

        with self.assertRaises(AuthorityMutationAmbiguousError):
            capability.execute("sqlite", ambiguous)
        with self.assertRaisesRegex(AuthorityMutationError, "already consumed"):
            capability.execute("sqlite", self._result)

    def test_backend_ambiguity_is_normalized_and_cannot_retry(self) -> None:
        for backend, error in (
            ("git", GitMutationAmbiguousError("git uncertain")),
            ("sqlite", SQLiteMutationAmbiguousError("sqlite uncertain")),
        ):
            capability = BoundAuthorityMutation(_admission(backend))

            def ambiguous(error: Exception = error) -> object:
                raise error

            with self.assertRaisesRegex(AuthorityMutationAmbiguousError, "outcome is ambiguous"):
                capability.execute(backend, ambiguous)
            with self.assertRaisesRegex(AuthorityMutationError, "already consumed"):
                capability.execute(backend, self._result)

    def test_termination_exception_is_ambiguous_and_cannot_retry(self) -> None:
        capability = BoundAuthorityMutation(_admission())

        def terminated() -> object:
            raise SystemExit(17)

        with self.assertRaisesRegex(AuthorityMutationAmbiguousError, "outcome is ambiguous"):
            capability.execute("git", terminated)
        with self.assertRaisesRegex(AuthorityMutationError, "already consumed"):
            capability.execute("git", self._result)

    def test_durable_effect_publishes_only_after_verified_receipt(self) -> None:
        journal: list[tuple[str, str]] = []

        class Journal:
            def prepare_authority_effect(
                self,
                expected_revision: int,
                operation_id: str,
                _backend: str,
                _target: str,
                *,
                expected_fencing_token: str | None = None,
                expected_barrier_id: str | None = None,
            ) -> str:
                del expected_fencing_token, expected_barrier_id
                self.expected_revision = expected_revision
                journal.append(("prepared", operation_id))
                return "intent-1"

            def finish_authority_effect(self, intent: object, outcome: str) -> None:
                self.intent = intent
                journal.append((outcome, str(intent)))

        receipt = DurableBoundAuthorityMutation(
            _admission(), Journal(), session_revision=1
        ).execute(lambda: self._result())
        self.assertTrue(receipt.mutates_authority)
        self.assertEqual([("prepared", "op-1:commit"), ("committed", "intent-1")], journal)

    def test_durable_effect_forwards_exact_admission_identity_to_journal(self) -> None:
        captured: list[object] = []

        class Journal:
            def prepare_authority_effect(
                self,
                expected_revision: int,
                operation_id: str,
                backend: str,
                target: str,
                *,
                expected_fencing_token: str | None = None,
                expected_barrier_id: str | None = None,
            ) -> str:
                captured.extend(
                    [
                        expected_revision,
                        operation_id,
                        backend,
                        target,
                        expected_fencing_token,
                        expected_barrier_id,
                    ]
                )
                return "intent-identity"

            def finish_authority_effect(self, _intent: object, _outcome: str) -> None:
                return None

        DurableBoundAuthorityMutation(_admission("sqlite"), Journal(), session_revision=1).execute(
            lambda: self._result()
        )
        self.assertEqual(
            [1, "op-1:commit", "sqlite", "new", "fence-1", "barrier-1"],
            captured,
        )

    def test_durable_ambiguous_effect_is_durably_marked_ambiguous(self) -> None:
        journal: list[str] = []

        class Journal:
            def prepare_authority_effect(
                self,
                _expected_revision: int,
                _operation_id: str,
                _backend: str,
                _target: str,
                *,
                expected_fencing_token: str | None = None,
                expected_barrier_id: str | None = None,
            ) -> str:
                del expected_fencing_token, expected_barrier_id
                return "intent-uncertain"

            def finish_authority_effect(self, intent: object, outcome: str) -> None:
                journal.append(f"{intent}:{outcome}")

        capability = DurableBoundAuthorityMutation(_admission(), Journal(), session_revision=1)
        with self.assertRaisesRegex(AuthorityMutationAmbiguousError, "outcome is ambiguous"):
            capability.execute(lambda: (_ for _ in ()).throw(TimeoutError("unknown")))
        self.assertEqual(["intent-uncertain:ambiguous"], journal)

    def test_durable_capability_fails_closed_if_ambiguity_cannot_be_journaled(self) -> None:
        class Journal:
            def prepare_authority_effect(
                self,
                _revision: int,
                _operation: str,
                _backend: str,
                _target: str,
                *,
                expected_fencing_token: str | None = None,
                expected_barrier_id: str | None = None,
            ) -> str:
                del expected_fencing_token, expected_barrier_id
                return "intent-journal-failure"

            def finish_authority_effect(self, _intent: object, _outcome: str) -> None:
                raise OSError("journal unavailable")

        with self.assertRaisesRegex(AuthorityMutationAmbiguousError, "recovery journal"):
            DurableBoundAuthorityMutation(_admission(), Journal(), session_revision=1).execute(
                lambda: (_ for _ in ()).throw(TimeoutError("unknown"))
            )

    def test_durable_capability_fails_closed_if_success_publication_is_uncertain(self) -> None:
        class Journal:
            def prepare_authority_effect(
                self,
                _revision: int,
                _operation: str,
                _backend: str,
                _target: str,
                *,
                expected_fencing_token: str | None = None,
                expected_barrier_id: str | None = None,
            ) -> str:
                del expected_fencing_token, expected_barrier_id
                return "intent-publication-failure"

            def finish_authority_effect(self, _intent: object, outcome: str) -> None:
                if outcome == "committed":
                    raise OSError("publication unavailable")

        with self.assertRaisesRegex(AuthorityMutationAmbiguousError, "publication"):
            DurableBoundAuthorityMutation(_admission(), Journal(), session_revision=1).execute(
                lambda: self._result()
            )


if __name__ == "__main__":
    unittest.main()
