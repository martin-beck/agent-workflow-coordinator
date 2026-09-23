# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile tests for the backend-neutral authority effect contract."""

from __future__ import annotations

import unittest

from tools.authority_mutation import (
    AuthorityMutationAmbiguousError,
    AuthorityMutationError,
    BoundAuthorityMutation,
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


if __name__ == "__main__":
    unittest.main()
