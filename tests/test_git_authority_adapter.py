# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile tests for the read-only Git authority adapter slice."""

# Test setup invokes fixed Git commands against an isolated temporary repository.
# ruff: noqa: S603, S607

from __future__ import annotations

import multiprocessing
import os
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from tools.admission_lease import AdmissionLease, validate_recheck
from tools.git_authority_adapter import (
    GitAuthorityAdapter,
    GitAuthorityError,
    GitRollbackSessionState,
)
from tools.handoffctl import locked
from tools.lock_domain import LockDomainContract
from tools.lock_domain_scope import LockDomainScope
from tools.mutation_fence import MutationFence, provision, provision_control_binding
from tools.rollback_control_store import (
    BarrierSessionState,
    ControlStoreError,
    SQLiteBarrierSessionStore,
    SQLiteRollbackControlStore,
)
from tools.scoped_backend_adapter import ScopedBackendAdapter
from tools.upgrade_engine import BoundRollbackCapability, PhaseContext, UpgradeEngine
from tools.upgrade_identity import (
    BarrierSessionIdentity,
    canonical_barrier_digest,
    canonical_barrier_session_digest,
    canonical_envelope_digest,
)

PROJECT = "11111111-1111-4111-8111-111111111111"

CONTEXT = {
    "schema_version": 2,
    "backend": "git",
    "project_id": PROJECT,
    "operation_id": "op-1",
    "state_revision": 1,
    "authority_revision": "authority",
    "fencing_token": "fence",
    "fencing_owner": "owner",
    "durable_barrier_id": "barrier",
    "artifact_root": "/artifacts",
    "source": "/source",
    "destination": "/destination",
    "manifest": "/manifest",
    "barrier_identity_digest": "0" * 64,
    "target": "new",
    "envelope_digest": "0" * 64,
}


def _bound_snapshot_process(
    repository_text: str,
    coordination_text: str,
    expected_branch: str,
    expected_head: str,
    mode: str,
    result: Any,
    rollback: bool = False,
) -> None:
    """Run one real bound snapshot in a fresh process.

    ``crash`` aborts from the first Git observation, after ``snapshot_bound``
    has entered the concrete common/control/authority scope. ``stale`` keeps
    the backend observable so a call would be reported, but must reject from
    the durable reread first.
    """
    coordination = Path(coordination_text)
    authority = coordination / "authority.sqlite"
    control = coordination / "control.sqlite"
    store = SQLiteRollbackControlStore(control, PROJECT, authority)
    session = SQLiteBarrierSessionStore(store, lambda: "authority")
    fence = MutationFence(
        authority,
        coordination / "authority-marker.json",
        coordination / "authority-lifecycle.json",
        coordination / "authority.lock",
        control,
        coordination / "control-binding.json",
        store.control_lock_path,
    )
    with locked() as guard:
        domain = LockDomainContract.capture(guard, session, fence)
    replacement = mode == "replacement"
    authority_revision = "authority"
    fencing_token = "fence-replaced" if replacement else "fence"
    fencing_owner = "owner-replaced" if replacement else "owner"
    durable_barrier_id = "barrier-replaced" if replacement else "barrier"
    state_revision = 2 if replacement else 1
    lease = AdmissionLease(
        PROJECT,
        authority_revision,
        fencing_token,
        fencing_owner,
        durable_barrier_id,
        state_revision,
    )
    recheck = validate_recheck(
        lease,
        project_id=PROJECT,
        authority_revision=authority_revision,
        fencing_token=fencing_token,
        fencing_owner=fencing_owner,
        durable_barrier_id=durable_barrier_id,
        revision=state_revision,
    )
    scope = LockDomainScope.bind(session, fence, lease, recheck, locked)
    adapter = GitAuthorityAdapter(Path(repository_text))
    adapter_any: Any = adapter

    if mode == "crash":

        def aborting_git(*_arguments: str) -> str:
            os._exit(17)

        adapter_any._git = aborting_git
    elif mode == "stale":
        calls = 0

        def unexpected_git(*_arguments: str) -> str:
            nonlocal calls
            calls += 1
            raise AssertionError("stale session reached Git backend")

        adapter_any._git = unexpected_git

    active_context = {
        **CONTEXT,
        "target": "rollback" if rollback else "new",
        "authority_revision": authority_revision,
        "fencing_token": fencing_token,
        "fencing_owner": fencing_owner,
        "durable_barrier_id": durable_barrier_id,
        "state_revision": state_revision,
    }
    try:
        if rollback:
            value = adapter.verify_rollback_context_bound(
                active_context,
                scope,
                lease=lease,
                admission_recheck=recheck,
                expected_branch=expected_branch,
                expected_head=expected_head,
            )
        else:
            value = adapter.snapshot_bound(
                "discover",
                active_context,
                scope,
                lease=lease,
                admission_recheck=recheck,
                expected_branch=expected_branch,
                expected_head=expected_head,
            )
    except Exception as error:
        result.put(("rejected", type(error).__name__, str(error), locals().get("calls", 0)))
    else:
        result.put(("success", value["git_head"], value["git_branch"]))


class GitAuthorityAdapterTests(unittest.TestCase):
    def test_bound_reread_returns_typed_session_without_cas_arguments(self) -> None:
        value = {
            **CONTEXT,
            "phase": "rollback",
            "backend_identity_verified": True,
            "git_head": "a" * 40,
            "git_branch": "main",
            "git_clean": True,
            "mutates_authority": False,
        }
        with patch.object(self.adapter, "snapshot_bound", return_value=value) as reread:
            result = self.adapter.snapshot_bound_reread(
                CONTEXT,
                cast(Any, object()),
                lease=cast(Any, object()),
                admission_recheck=cast(Any, object()),
                expected_branch="main",
                expected_head="a" * 40,
            )
        self.assertIsInstance(result, GitRollbackSessionState)
        self.assertEqual("main", result.git_branch)
        self.assertEqual("a" * 40, result.git_head)
        self.assertEqual("rollback", reread.call_args.args[0])

    @staticmethod
    def _lease() -> AdmissionLease:
        return AdmissionLease(PROJECT, "authority", "fence", "owner", "barrier", 1)

    @staticmethod
    def _session_identity() -> BarrierSessionIdentity:
        record: dict[str, object] = {
            "schema_version": 1,
            "project_id": PROJECT,
            "attempt_id": "attempt-1",
            "state_revision": 1,
            "authority_revision_at_acquire": "authority",
            "durable_barrier_id": "barrier",
            "fencing_token": "fence",
            "fencing_owner": "owner",
            "identity_digest": "0" * 64,
        }
        record["identity_digest"] = canonical_barrier_session_digest(record)
        return BarrierSessionIdentity.from_record(record)

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "state").write_text("clean\n")
        subprocess.run(["git", "-C", str(self.root), "add", "state"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example",
                "commit",
                "-qm",
                "init",
            ],
            check=True,
        )
        self.adapter = GitAuthorityAdapter(self.root)
        self.coordination = tempfile.TemporaryDirectory()
        coord = Path(self.coordination.name)
        authority = coord / "authority.sqlite"
        authority.write_bytes(b"authority")
        authority.chmod(0o600)
        control = coord / "control.sqlite"
        store = SQLiteRollbackControlStore(control, PROJECT, authority)
        marker = coord / "authority-marker.json"
        lifecycle = coord / "authority-lifecycle.json"
        authority_lock = coord / "authority.lock"
        binding = coord / "control-binding.json"
        provision(authority, marker, lifecycle, authority_lock, PROJECT)
        provision_control_binding(control, binding, store.control_lock_path, PROJECT)
        fence = MutationFence(
            authority,
            marker,
            lifecycle,
            authority_lock,
            control,
            binding,
            store.control_lock_path,
        )
        self.session = SQLiteBarrierSessionStore(store, lambda: "authority")
        self.session.create(self._session_identity())
        self.lease = self._lease()
        self.recheck = validate_recheck(
            self.lease,
            project_id=PROJECT,
            authority_revision="authority",
            fencing_token="fence",  # noqa: S106
            fencing_owner="owner",
            durable_barrier_id="barrier",
            revision=1,
        )
        self.scope = LockDomainScope.bind(self.session, fence, self.lease, self.recheck, locked)

    def tearDown(self) -> None:
        self.coordination.cleanup()
        self.directory.cleanup()

    def test_engine_bound_rollback_inspection_uses_real_scope_and_preserves_journal(self) -> None:
        context = {**CONTEXT, "operation_id": "op-real-git-inspection", "target": "new"}
        artifact_root = Path(self.coordination.name) / "artifacts"
        artifact_root.mkdir()
        context.update(
            {
                "artifact_root": str(artifact_root),
                "destination": str(artifact_root / "destination"),
                "manifest": str(artifact_root / "manifest.json"),
            }
        )
        context["barrier_identity_digest"] = canonical_barrier_digest(context)
        context["envelope_digest"] = canonical_envelope_digest(context)
        observed = self.adapter.snapshot("discover", context)
        context["target"] = "new"
        engine = UpgradeEngine(
            str(context["operation_id"]),
            Path(self.coordination.name) / "engine-journal.json",
            context,
            backend_adapter=self.adapter,
            rollback_bound_verifier=BoundRollbackCapability.bind(
                PhaseContext(**cast(dict[str, Any], context)),
                self.adapter,
                self.scope,
                lease=self.lease,
                admission_recheck=self.recheck,
                expected_branch=str(observed["git_branch"]),
                expected_head=str(observed["git_head"]),
            ),
        )
        engine.plan()
        before = (Path(self.coordination.name) / "engine-journal.json").read_bytes()
        result = engine.inspect_rollback_bound()
        self.assertFalse(result["rollback_context_verified"])
        self.assertEqual(
            before, (Path(self.coordination.name) / "engine-journal.json").read_bytes()
        )
        self.assertFalse(self.session.operation_owned_by_current_thread)
        with engine._exclusive():
            pass

    def test_clean_snapshot_is_identity_bound_and_nonmutating(self) -> None:
        result = self.adapter.snapshot("discover", CONTEXT)
        self.assertTrue(result["backend_identity_verified"])
        self.assertTrue(result["git_clean"])
        self.assertFalse(result["mutates_authority"])
        for phase in ("", "unknown", 1, True):
            with (
                self.subTest(phase=phase),
                self.assertRaisesRegex(GitAuthorityError, "phase is invalid"),
            ):
                self.adapter.snapshot(cast(Any, phase), CONTEXT)
        durable_before = self.session.snapshot()
        with (
            patch.object(self.adapter, "_git", side_effect=AssertionError("Git reached")) as git,
            patch.object(
                self.adapter, "execute", side_effect=AssertionError("execute reached")
            ) as execute,
            self.assertRaisesRegex(GitAuthorityError, "rollback context target"),
        ):
            self.adapter.verify_rollback_context(CONTEXT)
        git.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(durable_before, self.session.snapshot())
        self.assertFalse(self.session.operation_owned_by_current_thread)
        rollback_context = {**CONTEXT, "target": "rollback"}
        self.assertFalse(
            self.adapter.verify_rollback_context(rollback_context)["rollback_context_verified"]
        )
        bound = self.adapter.verify_rollback_context_bound(
            rollback_context,
            self.scope,
            lease=self.lease,
            admission_recheck=self.recheck,
            expected_branch=str(result["git_branch"]),
            expected_head=str(result["git_head"]),
        )
        self.assertEqual("rollback", bound["phase"])
        self.assertFalse(bound["rollback_context_verified"])
        self.assertFalse(self.session.operation_owned_by_current_thread)
        durable_before_bound_reject = self.session.snapshot()
        with (
            patch.object(self.adapter, "_git", side_effect=AssertionError("Git reached")) as git,
            patch.object(
                self.adapter, "execute", side_effect=AssertionError("execute reached")
            ) as execute,
            self.assertRaisesRegex(GitAuthorityError, "rollback context target"),
        ):
            self.adapter.verify_rollback_context_bound(
                CONTEXT,
                self.scope,
                lease=self.lease,
                admission_recheck=self.recheck,
                expected_branch=str(result["git_branch"]),
                expected_head=str(result["git_head"]),
            )
        git.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(durable_before_bound_reject, self.session.snapshot())
        self.assertFalse(self.session.operation_owned_by_current_thread)
        with self.assertRaisesRegex(GitAuthorityError, "not implemented"):
            self.adapter.execute("commit", CONTEXT)

    def test_git_observation_failures_and_invalid_repository_fail_closed(self) -> None:
        with self.assertRaisesRegex(GitAuthorityError, "unavailable"):
            GitAuthorityAdapter(self.root / "missing")
        with (
            patch("tools.git_authority_adapter.subprocess.run", side_effect=OSError("git")),
            self.assertRaisesRegex(GitAuthorityError, "observation failed"),
        ):
            self.adapter._git("status")
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="rejected")
        with (
            patch("tools.git_authority_adapter.subprocess.run", return_value=failed),
            self.assertRaisesRegex(GitAuthorityError, "observation was rejected"),
        ):
            self.adapter._git("status")
        with (
            patch.object(self.adapter, "_git", side_effect=("", "", "")),
            self.assertRaisesRegex(GitAuthorityError, "not clean"),
        ):
            self.adapter.snapshot("discover", CONTEXT)

    def test_snapshot_bound_validates_concrete_scope_and_admission_inputs(self) -> None:
        observed = self.adapter.snapshot("discover", CONTEXT)
        common = {
            "phase": "discover",
            "context": CONTEXT,
            "scope": self.scope,
            "lease": self.lease,
            "admission_recheck": self.recheck,
            "expected_branch": str(observed["git_branch"]),
            "expected_head": str(observed["git_head"]),
        }
        bound = cast(Any, self.adapter.snapshot_bound)
        with self.assertRaisesRegex(GitAuthorityError, "branch identity"):
            bound(**{**common, "expected_branch": ""})
        with self.assertRaisesRegex(GitAuthorityError, "head identity"):
            bound(**{**common, "expected_head": ""})
        with self.assertRaisesRegex(GitAuthorityError, "lease"):
            bound(**{**common, "lease": cast(Any, object())})
        with self.assertRaisesRegex(GitAuthorityError, "recheck"):
            bound(**{**common, "admission_recheck": cast(Any, object())})
        with self.assertRaisesRegex(GitAuthorityError, "recheck"):
            bound(
                **{
                    **common,
                    "admission_recheck": validate_recheck(
                        AdmissionLease(PROJECT, "authority", "other", "owner", "barrier", 1),
                        project_id=PROJECT,
                        authority_revision="authority",
                        fencing_token="other",  # noqa: S106
                        fencing_owner="owner",
                        durable_barrier_id="barrier",
                        revision=1,
                    ),
                }
            )
        with self.assertRaisesRegex(GitAuthorityError, "concrete lock-domain"):
            bound(**{**common, "scope": cast(Any, object())})
        for phase in ("", "unknown", 1, True):
            with (
                self.subTest(bound_phase=phase),
                patch.object(self.adapter, "_git") as git,
                self.assertRaisesRegex(GitAuthorityError, "phase is invalid"),
            ):
                bound(**{**common, "phase": cast(Any, phase)})
            git.assert_not_called()
        self.assertFalse(self.session.operation_owned_by_current_thread)
        with (
            patch.object(self.adapter, "_git", wraps=self.adapter._git) as git,
            self.assertRaisesRegex(GitAuthorityError, "phase is invalid"),
        ):
            bound(**{**common, "phase": ""})
        git.assert_not_called()

    def test_snapshot_bound_keeps_full_backend_schema_after_scope_binding(self) -> None:
        observed = self.adapter.snapshot("discover", CONTEXT)
        invalid_contexts = [
            {key: value for key, value in CONTEXT.items() if key != "operation_id"},
            {**CONTEXT, "operation_id": 3},
            {**CONTEXT, "unexpected": "hostile"},
        ]
        for invalid in invalid_contexts:
            with patch.object(self.adapter, "_git", wraps=self.adapter._git) as git:
                with (
                    self.subTest(context=repr(invalid)),
                    self.assertRaisesRegex(
                        GitAuthorityError, "incomplete or mismatched|types are invalid"
                    ),
                ):
                    self.adapter.snapshot_bound(
                        "discover",
                        invalid,
                        self.scope,
                        lease=self.lease,
                        admission_recheck=self.recheck,
                        expected_branch=str(observed["git_branch"]),
                        expected_head=str(observed["git_head"]),
                    )
                git.assert_not_called()

    def test_scoped_wrapper_is_the_only_composed_mutation_boundary(self) -> None:
        class Scope:
            def assert_ordered(self) -> None:
                pass

            def assert_context(self, _context: Mapping[str, object]) -> None:
                pass

            def hold(self) -> AbstractContextManager[object]:
                return nullcontext()

        adapter = ScopedBackendAdapter(self.adapter, Scope())
        self.assertTrue(adapter.snapshot("discover", CONTEXT)["git_clean"])
        with self.assertRaisesRegex(TypeError, "disabled"):
            adapter.execute("commit", CONTEXT)

    def test_snapshot_bound_rechecks_session_and_binds_immutable_git_identity(self) -> None:
        observed = self.adapter.snapshot("discover", CONTEXT)
        bound = self.adapter.snapshot_bound(
            "discover",
            CONTEXT,
            self.scope,
            lease=self._lease(),
            admission_recheck=self.recheck,
            expected_branch=str(observed["git_branch"]),
            expected_head=str(observed["git_head"]),
        )
        self.assertEqual(observed["git_head"], bound["git_head"])
        self.assertFalse(bound["mutates_authority"])

    def test_snapshot_bound_reread_real_session_and_rejects_foreign_head(self) -> None:
        observed = self.adapter.snapshot("rollback", CONTEXT)
        result = self.adapter.snapshot_bound_reread(
            CONTEXT,
            self.scope,
            lease=self.lease,
            admission_recheck=self.recheck,
            expected_branch=str(observed["git_branch"]),
            expected_head=str(observed["git_head"]),
        )
        self.assertEqual(str(observed["git_head"]), result.git_head)
        self.assertEqual(str(observed["git_branch"]), result.git_branch)
        with self.assertRaisesRegex(GitAuthorityError, "identity changed"):
            self.adapter.snapshot_bound_reread(
                CONTEXT,
                self.scope,
                lease=self.lease,
                admission_recheck=self.recheck,
                expected_branch=str(observed["git_branch"]),
                expected_head="0" * 40,
            )

    def test_snapshot_bound_rejects_session_or_identity_drift_before_observation(self) -> None:
        observed = self.adapter.snapshot("discover", CONTEXT)
        with self.assertRaisesRegex(GitAuthorityError, "identity changed"):
            self.adapter.snapshot_bound(
                "discover",
                CONTEXT,
                self.scope,
                lease=self._lease(),
                admission_recheck=self.recheck,
                expected_branch=str(observed["git_branch"]),
                expected_head="0" * 40,
            )
        self.session.mark_ambiguous(1, "stale-session")
        with (
            patch.object(self.adapter, "_git", wraps=self.adapter._git) as git,
            self.assertRaisesRegex(GitAuthorityError, "trusted Git session"),
        ):
            self.adapter.snapshot_bound(
                "discover",
                CONTEXT,
                self.scope,
                lease=self._lease(),
                admission_recheck=self.recheck,
                expected_branch=str(observed["git_branch"]),
                expected_head=str(observed["git_head"]),
            )
        git.assert_not_called()

    def test_dirty_or_detached_or_mismatched_context_fails_closed(self) -> None:
        (self.root / "state").write_text("dirty\n")
        with self.assertRaisesRegex(GitAuthorityError, "not clean"):
            self.adapter.snapshot("discover", CONTEXT)
        (self.root / "state").write_text("clean\n")
        with self.assertRaisesRegex(GitAuthorityError, "mismatched"):
            self.adapter.snapshot("discover", {**CONTEXT, "backend": "sqlite"})

    def test_snapshot_bound_rejects_durable_identity_drift_before_scope(self) -> None:
        observed = self.adapter.snapshot("discover", CONTEXT)
        for field in (
            "project_id",
            "authority_revision",
            "fencing_token",
            "fencing_owner",
            "durable_barrier_id",
            "state_revision",
        ):
            drifted = dict(CONTEXT)
            drifted[field] = 2 if field == "state_revision" else f"changed-{field}"
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(GitAuthorityError, "session identity"),
            ):
                self.adapter.snapshot_bound(
                    "discover",
                    drifted,
                    self.scope,
                    lease=self._lease(),
                    admission_recheck=self.recheck,
                    expected_branch=str(observed["git_branch"]),
                    expected_head=str(observed["git_head"]),
                )

    def test_snapshot_bound_rejects_replaced_lease_before_backend(self) -> None:
        observed = self.adapter.snapshot("discover", CONTEXT)
        replaced = AdmissionLease(PROJECT, "authority", "replacement", "owner", "barrier", 1)
        with patch.object(self.adapter, "_git", wraps=self.adapter._git) as git:
            with self.assertRaisesRegex(GitAuthorityError, "recheck|session identity"):
                self.adapter.snapshot_bound(
                    "discover",
                    CONTEXT,
                    self.scope,
                    lease=replaced,
                    admission_recheck=self.recheck,
                    expected_branch=str(observed["git_branch"]),
                    expected_head=str(observed["git_head"]),
                )
            git.assert_not_called()

    def test_snapshot_bound_rejects_full_context_schema_before_scope(self) -> None:
        class ScopeMustNotRun:
            def assert_ordered(self) -> None:
                raise AssertionError("scope must not run")

            def assert_context(self, _context: Mapping[str, object]) -> None:
                raise AssertionError("scope must not run")

            def hold(self) -> AbstractContextManager[object]:
                raise AssertionError("scope must not run")

        observed = self.adapter.snapshot("discover", CONTEXT)
        invalid_contexts = [
            {key: value for key, value in CONTEXT.items() if key != "operation_id"},
            {**CONTEXT, "unexpected": "hostile"},
            {**CONTEXT, "state_revision": True},
        ]
        for invalid in invalid_contexts:
            with (
                self.subTest(context=repr(invalid)),
                self.assertRaisesRegex(GitAuthorityError, "context"),
            ):
                self.adapter.snapshot_bound(
                    "discover",
                    invalid,
                    cast(Any, ScopeMustNotRun()),
                    lease=self._lease(),
                    admission_recheck=self.recheck,
                    expected_branch=str(observed["git_branch"]),
                    expected_head=str(observed["git_head"]),
                )

    def test_process_abort_releases_bound_scope_for_fresh_read_only_worker(self) -> None:
        """A child abort inside snapshot_bound leaves all locks reusable."""
        observed = self.adapter.snapshot("discover", CONTEXT)
        context = multiprocessing.get_context("fork")
        crashed_result = context.Queue()
        crashed = context.Process(
            target=_bound_snapshot_process,
            args=(
                str(self.root),
                self.coordination.name,
                str(observed["git_branch"]),
                str(observed["git_head"]),
                "crash",
                crashed_result,
                True,
            ),
        )
        crashed.start()
        crashed.join(5)
        self.assertEqual(17, crashed.exitcode)

        recovered_result = context.Queue()
        recovered = context.Process(
            target=_bound_snapshot_process,
            args=(
                str(self.root),
                self.coordination.name,
                str(observed["git_branch"]),
                str(observed["git_head"]),
                "success",
                recovered_result,
                True,
            ),
        )
        recovered.start()
        recovered.join(5)
        self.assertEqual(0, recovered.exitcode)
        self.assertEqual(
            ("success", observed["git_head"], observed["git_branch"]),
            recovered_result.get(timeout=1),
        )

    def test_typed_session_transition_rejects_then_reacquires_fresh_worker(self) -> None:
        """Typed ambiguous/replacement transitions gate fresh bound workers."""
        observed = self.adapter.snapshot("discover", CONTEXT)

        def fail_publication(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("intent publication unavailable")

        with (
            patch.object(self.session, "_mark_intent_locked", side_effect=fail_publication),
            self.assertRaisesRegex(ControlStoreError, "outcome publication is ambiguous"),
        ):
            self.session.cas(1, BarrierSessionState(self._session_identity(), "held", 2))
        ambiguous = self.session.recover_unknown()
        self.assertIsNotNone(ambiguous)
        assert ambiguous is not None
        self.assertEqual("ambiguous", ambiguous.status)
        durable_before_failures = self.session.snapshot()

        context = multiprocessing.get_context("fork")
        result = context.Queue()
        fresh = context.Process(
            target=_bound_snapshot_process,
            args=(
                str(self.root),
                self.coordination.name,
                str(observed["git_branch"]),
                str(observed["git_head"]),
                "stale",
                result,
            ),
        )
        fresh.start()
        fresh.join(5)
        self.assertEqual(0, fresh.exitcode)
        outcome = result.get(timeout=1)
        self.assertEqual("rejected", outcome[0])
        self.assertIn(outcome[1], {"GitAuthorityError", "LockDomainError"})
        self.assertEqual(0, outcome[3])
        self.assertFalse(self.session.operation_owned_by_current_thread)
        self.assertEqual(durable_before_failures, self.session.snapshot())

        replacement_record = self._session_identity().as_record()
        replacement_record.update(
            {
                "attempt_id": "attempt-replacement",
                "state_revision": 2,
                "durable_barrier_id": "barrier-replaced",
                "fencing_token": "fence-replaced",
                "fencing_owner": "owner-replaced",
            }
        )
        replacement_record["identity_digest"] = canonical_barrier_session_digest(replacement_record)
        replacement = BarrierSessionState(
            BarrierSessionIdentity.from_record(replacement_record), "held", 1
        )
        with self.assertRaisesRegex(ControlStoreError, "CAS conflict"):
            self.session.reconcile_ambiguous(ambiguous.revision - 1, replacement)
        self.assertFalse(self.session.operation_owned_by_current_thread)
        self.assertEqual(durable_before_failures, self.session.snapshot())
        for field in ("attempt_id", "durable_barrier_id", "fencing_token"):
            reused_record = dict(replacement_record)
            reused_record[field] = self._session_identity().as_record()[field]
            reused_record["identity_digest"] = canonical_barrier_session_digest(reused_record)
            reused = BarrierSessionState(
                BarrierSessionIdentity.from_record(reused_record), "held", 1
            )

            with (
                self.subTest(reused_field=field),
                self.assertRaisesRegex(ControlStoreError, "distinct newer fence"),
            ):
                self.session.reconcile_ambiguous(ambiguous.revision, reused)
            self.assertFalse(self.session.operation_owned_by_current_thread)
            self.assertEqual(durable_before_failures, self.session.snapshot())
        mismatch_store = SQLiteBarrierSessionStore(
            SQLiteRollbackControlStore(
                self.session.control_store_path, PROJECT, self.session.authority_path
            ),
            lambda: "authority-mismatch",
        )
        with self.assertRaisesRegex(ControlStoreError, "replacement authority revision changed"):
            mismatch_store.reconcile_ambiguous(ambiguous.revision, replacement)
        self.assertFalse(mismatch_store.operation_owned_by_current_thread)
        self.assertEqual(durable_before_failures, self.session.snapshot())
        self.assertEqual(
            replacement, self.session.reconcile_ambiguous(ambiguous.revision, replacement)
        )

        recovered_result = context.Queue()
        recovered = context.Process(
            target=_bound_snapshot_process,
            args=(
                str(self.root),
                self.coordination.name,
                str(observed["git_branch"]),
                str(observed["git_head"]),
                "replacement",
                recovered_result,
            ),
        )
        recovered.start()
        recovered.join(5)
        self.assertEqual(0, recovered.exitcode)
        self.assertEqual(
            ("success", observed["git_head"], observed["git_branch"]),
            recovered_result.get(timeout=1),
        )

    def test_snapshot_bound_rejects_backend_equivalence_drift(self) -> None:
        class TamperedAdapter(GitAuthorityAdapter):
            def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, Any]:
                value = super().snapshot(phase, context)
                value["project_id"] = "tampered"
                return value

        observed = self.adapter.snapshot("discover", CONTEXT)
        tampered = TamperedAdapter(self.root)
        with (
            patch.object(tampered, "execute", side_effect=AssertionError("execute reached")),
            self.assertRaisesRegex(GitAuthorityError, "backend context identity"),
        ):
            tampered.snapshot_bound(
                "discover",
                CONTEXT,
                self.scope,
                lease=self._lease(),
                admission_recheck=self.recheck,
                expected_branch=str(observed["git_branch"]),
                expected_head=str(observed["git_head"]),
            )
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_snapshot_bound_rejects_non_read_only_backend_result(self) -> None:
        class TamperedAdapter(GitAuthorityAdapter):
            def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, Any]:
                value = super().snapshot(phase, context)
                value["mutates_authority"] = True
                return value

        observed = self.adapter.snapshot("discover", CONTEXT)
        tampered = TamperedAdapter(self.root)
        with (
            patch.object(tampered, "execute", side_effect=AssertionError("execute reached")),
            self.assertRaisesRegex(GitAuthorityError, "not read-only"),
        ):
            tampered.snapshot_bound(
                "discover",
                CONTEXT,
                self.scope,
                lease=self._lease(),
                admission_recheck=self.recheck,
                expected_branch=str(observed["git_branch"]),
                expected_head=str(observed["git_head"]),
            )
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_snapshot_bound_rejects_phase_or_cleanliness_drift(self) -> None:
        class TamperedAdapter(GitAuthorityAdapter):
            field = "phase"

            def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, Any]:
                value = super().snapshot(phase, context)
                value[self.field] = "other" if self.field == "phase" else False
                return value

        observed = self.adapter.snapshot("discover", CONTEXT)
        for field, message in (
            ("phase", "backend phase changed"),
            ("git_clean", "cleanliness is unverified"),
        ):
            with self.subTest(field=field):
                tampered = type("FieldTamperedAdapter", (TamperedAdapter,), {"field": field})(
                    self.root
                )
                with (
                    patch.object(
                        tampered, "execute", side_effect=AssertionError("execute reached")
                    ),
                    self.assertRaisesRegex(GitAuthorityError, message),
                ):
                    tampered.snapshot_bound(
                        "discover",
                        CONTEXT,
                        self.scope,
                        lease=self._lease(),
                        admission_recheck=self.recheck,
                        expected_branch=str(observed["git_branch"]),
                        expected_head=str(observed["git_head"]),
                    )
                self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_snapshot_bound_requires_exact_result_schema_and_types(self) -> None:
        fields = {
            "phase": 1,
            "backend_identity_verified": 1,
            "git_head": 1,
            "git_branch": 1,
            "git_clean": 1,
            "mutates_authority": 0,
        }

        class TamperedAdapter(GitAuthorityAdapter):
            mode = "wrong"
            field = "phase"
            wrong: object = 1

            def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, Any]:
                value = super().snapshot(phase, context)
                if self.mode == "missing":
                    value.pop(self.field)
                elif self.mode == "extra":
                    value["unexpected"] = True
                else:
                    value[self.field] = self.wrong
                return value

        observed = self.adapter.snapshot("discover", CONTEXT)
        for mode in ("missing", "extra", "wrong"):
            for field, wrong in fields.items():
                with self.subTest(mode=mode, field=field):
                    adapter = type(
                        "SchemaTamperedAdapter",
                        (TamperedAdapter,),
                        {"mode": mode, "field": field, "wrong": wrong},
                    )(self.root)
                    with (
                        patch.object(
                            adapter, "execute", side_effect=AssertionError("execute reached")
                        ),
                        self.assertRaises(GitAuthorityError),
                    ):
                        adapter.snapshot_bound(
                            "discover",
                            CONTEXT,
                            self.scope,
                            lease=self._lease(),
                            admission_recheck=self.recheck,
                            expected_branch=str(observed["git_branch"]),
                            expected_head=str(observed["git_head"]),
                        )
                    self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_snapshot_bound_rejects_boolean_integer_context_substitution(self) -> None:
        class TamperedAdapter(GitAuthorityAdapter):
            def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, Any]:
                value = super().snapshot(phase, context)
                value["state_revision"] = True
                return value

        observed = self.adapter.snapshot("discover", CONTEXT)
        tampered = TamperedAdapter(self.root)
        with (
            patch.object(tampered, "execute", side_effect=AssertionError("execute reached")),
            patch.object(tampered, "_git", wraps=tampered._git) as git,
            self.assertRaisesRegex(GitAuthorityError, "backend context identity"),
        ):
            tampered.snapshot_bound(
                "discover",
                CONTEXT,
                self.scope,
                lease=self._lease(),
                admission_recheck=self.recheck,
                expected_branch=str(observed["git_branch"]),
                expected_head=str(observed["git_head"]),
            )
        self.assertEqual(3, git.call_count)
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_typed_reconcile_rejects_real_unresolved_intent(self) -> None:
        """A prepared intent left by publication failure blocks replacement."""

        def fail_publication(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("intent publication unavailable")

        with (
            patch.object(self.session, "_mark_intent_locked", side_effect=fail_publication),
            self.assertRaisesRegex(ControlStoreError, "outcome publication is ambiguous"),
        ):
            self.session.cas(1, BarrierSessionState(self._session_identity(), "held", 2))
        with (
            patch.object(self.session, "_mark_intent_locked", side_effect=fail_publication),
            self.assertRaisesRegex(OSError, "intent publication unavailable"),
        ):
            self.session.recover_unknown()
        current = self.session.snapshot()
        self.assertIsNotNone(current)
        assert current is not None
        replacement_record = self._session_identity().as_record()
        replacement_record.update(
            {
                "attempt_id": "attempt-newer",
                "state_revision": 3,
                "durable_barrier_id": "barrier-newer",
                "fencing_token": "fence-newer",
            }
        )
        replacement_record["identity_digest"] = canonical_barrier_session_digest(replacement_record)
        replacement = BarrierSessionState(
            BarrierSessionIdentity.from_record(replacement_record), "held", 1
        )
        with self.assertRaisesRegex(ControlStoreError, "unresolved intent"):
            self.session.reconcile_ambiguous(current.revision, replacement)
        self.assertFalse(self.session.operation_owned_by_current_thread)
        self.assertEqual(current, self.session.snapshot())

    def test_rollback_recheck_never_authorizes_mutation(self) -> None:
        result = self.adapter.verify_rollback_context({**CONTEXT, "target": "rollback"})
        self.assertFalse(result["rollback_context_verified"])

    def test_bound_rollback_rejects_stale_and_replaced_sessions_before_git(self) -> None:
        observed = self.adapter.snapshot("discover", CONTEXT)
        rollback = {**CONTEXT, "target": "rollback"}
        ambiguous = self.session.mark_ambiguous(1, "rollback-stale")
        before_stale = self.session.snapshot()
        with (
            patch.object(self.adapter, "_git", side_effect=AssertionError("Git reached")) as git,
            patch.object(
                self.adapter, "execute", side_effect=AssertionError("execute reached")
            ) as execute,
            self.assertRaisesRegex(GitAuthorityError, "trusted Git session"),
        ):
            self.adapter.verify_rollback_context_bound(
                rollback,
                self.scope,
                lease=self.lease,
                admission_recheck=self.recheck,
                expected_branch=str(observed["git_branch"]),
                expected_head=str(observed["git_head"]),
            )
        git.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(before_stale, self.session.snapshot())
        self.assertFalse(self.session.operation_owned_by_current_thread)

        replacement_record = self._session_identity().as_record()
        replacement_record.update(
            {
                "attempt_id": "rollback-replacement",
                "state_revision": 2,
                "durable_barrier_id": "barrier-replacement",
                "fencing_token": "fence-replacement",
                "fencing_owner": "owner-replacement",
            }
        )
        replacement_record["identity_digest"] = canonical_barrier_session_digest(replacement_record)
        replacement = BarrierSessionState(
            BarrierSessionIdentity.from_record(replacement_record), "held", 1
        )
        self.session.reconcile_ambiguous(ambiguous.revision, replacement)
        before_replaced = self.session.snapshot()
        with (
            patch.object(self.adapter, "_git", side_effect=AssertionError("Git reached")) as git,
            patch.object(
                self.adapter, "execute", side_effect=AssertionError("execute reached")
            ) as execute,
            self.assertRaisesRegex(GitAuthorityError, "trusted Git session"),
        ):
            self.adapter.verify_rollback_context_bound(
                rollback,
                self.scope,
                lease=self.lease,
                admission_recheck=self.recheck,
                expected_branch=str(observed["git_branch"]),
                expected_head=str(observed["git_head"]),
            )
        git.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(before_replaced, self.session.snapshot())
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bound_rollback_rejects_tampered_git_results_without_state_change(self) -> None:
        observed = self.adapter.snapshot("discover", CONTEXT)
        rollback = {**CONTEXT, "target": "rollback"}

        class TamperedAdapter(GitAuthorityAdapter):
            field = "phase"
            replacement: object = "discover"

            def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, object]:
                value = super().snapshot(phase, context)
                value[self.field] = self.replacement
                return value

        fields = (
            ("phase", "discover", "backend phase changed"),
            ("project_id", "tampered", "backend context identity changed"),
            ("backend_identity_verified", False, "identity is unverified"),
            ("git_clean", False, "cleanliness is unverified"),
            ("mutates_authority", True, "not read-only"),
        )
        for field, replacement, message in fields:
            with self.subTest(field=field):
                adapter = type(
                    "RollbackTamperedGitAdapter",
                    (TamperedAdapter,),
                    {"field": field, "replacement": replacement},
                )(self.root)
                before = self.session.snapshot()
                with (
                    patch.object(
                        adapter, "execute", side_effect=AssertionError("execute reached")
                    ) as execute,
                    self.assertRaisesRegex(GitAuthorityError, message),
                ):
                    adapter.verify_rollback_context_bound(
                        rollback,
                        self.scope,
                        lease=self.lease,
                        admission_recheck=self.recheck,
                        expected_branch=str(observed["git_branch"]),
                        expected_head=str(observed["git_head"]),
                    )
                execute.assert_not_called()
                self.assertEqual(before, self.session.snapshot())
                self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bound_rollback_rejects_authority_and_backend_drift_without_execute(self) -> None:
        observed = self.adapter.snapshot("discover", CONTEXT)
        rollback = {**CONTEXT, "target": "rollback"}
        drifted = AdmissionLease(PROJECT, "authority-drift", "fence", "owner", "barrier", 1)
        drifted_recheck = validate_recheck(
            drifted,
            project_id=PROJECT,
            authority_revision="authority-drift",
            fencing_token="fence",  # noqa: S106
            fencing_owner="owner",
            durable_barrier_id="barrier",
            revision=1,
        )
        before = self.session.snapshot()
        with (
            patch.object(self.adapter, "_git", side_effect=AssertionError("Git reached")) as git,
            patch.object(
                self.adapter, "execute", side_effect=AssertionError("execute reached")
            ) as execute,
            self.assertRaisesRegex(GitAuthorityError, "trusted Git session reread"),
        ):
            self.adapter.verify_rollback_context_bound(
                {**rollback, "authority_revision": "authority-drift"},
                self.scope,
                lease=drifted,
                admission_recheck=drifted_recheck,
                expected_branch=str(observed["git_branch"]),
                expected_head=str(observed["git_head"]),
            )
        git.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(before, self.session.snapshot())
        self.assertFalse(self.session.operation_owned_by_current_thread)

        with (
            patch.object(
                self.adapter, "execute", side_effect=AssertionError("execute reached")
            ) as execute,
            self.assertRaisesRegex(GitAuthorityError, "Git authority identity changed"),
        ):
            self.adapter.verify_rollback_context_bound(
                rollback,
                self.scope,
                lease=self.lease,
                admission_recheck=self.recheck,
                expected_branch=str(observed["git_branch"]),
                expected_head="0" * 40,
            )
        execute.assert_not_called()
        self.assertEqual(before, self.session.snapshot())
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bound_rollback_rejects_malformed_admission_before_git(self) -> None:
        observed = self.adapter.snapshot("discover", CONTEXT)
        common = {
            "context": {**CONTEXT, "target": "rollback"},
            "scope": self.scope,
            "lease": self.lease,
            "admission_recheck": self.recheck,
            "expected_branch": str(observed["git_branch"]),
            "expected_head": str(observed["git_head"]),
        }
        bound = cast(Any, self.adapter.verify_rollback_context_bound)
        cases = (
            ("lease", cast(Any, object()), "trusted admission lease"),
            ("admission_recheck", cast(Any, object()), "trusted admission recheck"),
            ("scope", cast(Any, object()), "concrete lock-domain scope"),
            ("context", {**CONTEXT, "target": "unexpected"}, "rollback context target"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                before = self.session.snapshot()
                with (
                    patch.object(
                        self.adapter, "_git", side_effect=AssertionError("Git reached")
                    ) as git,
                    patch.object(
                        self.adapter, "execute", side_effect=AssertionError("execute reached")
                    ) as execute,
                    self.assertRaisesRegex(GitAuthorityError, message),
                ):
                    bound(**{**common, field: value})
                git.assert_not_called()
                execute.assert_not_called()
                self.assertEqual(before, self.session.snapshot())
                self.assertFalse(self.session.operation_owned_by_current_thread)


if __name__ == "__main__":
    unittest.main()
