# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile tests for the read-only Git authority adapter slice."""

# Test setup invokes fixed Git commands against an isolated temporary repository.
# ruff: noqa: S603, S607

from __future__ import annotations

import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from tools.admission_lease import AdmissionLease, validate_recheck
from tools.git_authority_adapter import GitAuthorityAdapter, GitAuthorityError
from tools.handoffctl import locked
from tools.lock_domain_scope import LockDomainScope
from tools.mutation_fence import MutationFence, provision, provision_control_binding
from tools.rollback_control_store import SQLiteBarrierSessionStore, SQLiteRollbackControlStore
from tools.scoped_backend_adapter import ScopedBackendAdapter
from tools.upgrade_identity import BarrierSessionIdentity, canonical_barrier_session_digest

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


class GitAuthorityAdapterTests(unittest.TestCase):
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

    def test_clean_snapshot_is_identity_bound_and_nonmutating(self) -> None:
        result = self.adapter.snapshot("discover", CONTEXT)
        self.assertTrue(result["backend_identity_verified"])
        self.assertTrue(result["git_clean"])
        self.assertFalse(result["mutates_authority"])
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
        with patch.object(self.adapter, "_git", wraps=self.adapter._git) as git:
            with self.assertRaisesRegex(GitAuthorityError, "trusted Git session"):
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

    def test_rollback_recheck_never_authorizes_mutation(self) -> None:
        result = self.adapter.verify_rollback_context(CONTEXT)
        self.assertFalse(result["rollback_context_verified"])


if __name__ == "__main__":
    unittest.main()
