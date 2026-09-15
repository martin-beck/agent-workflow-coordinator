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

from tools.admission_lease import AdmissionLease
from tools.git_authority_adapter import GitAuthorityAdapter, GitAuthorityError
from tools.scoped_backend_adapter import ScopedBackendAdapter

CONTEXT = {
    "schema_version": 2,
    "backend": "git",
    "project_id": "project",
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
        return AdmissionLease("project", "authority", "fence", "owner", "barrier", 1)

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

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_clean_snapshot_is_identity_bound_and_nonmutating(self) -> None:
        result = self.adapter.snapshot("discover", CONTEXT)
        self.assertTrue(result["backend_identity_verified"])
        self.assertTrue(result["git_clean"])
        self.assertFalse(result["mutates_authority"])
        with self.assertRaisesRegex(GitAuthorityError, "not implemented"):
            self.adapter.execute("commit", CONTEXT)

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
        class Scope:
            def assert_ordered(self) -> None:
                pass

            def assert_context(self, _context: Mapping[str, object]) -> None:
                pass

            def hold(self) -> AbstractContextManager[object]:
                return nullcontext()

        observed = self.adapter.snapshot("discover", CONTEXT)
        bound = self.adapter.snapshot_bound(
            "discover",
            CONTEXT,
            Scope(),
            lease=self._lease(),
            expected_branch=str(observed["git_branch"]),
            expected_head=str(observed["git_head"]),
        )
        self.assertEqual(observed["git_head"], bound["git_head"])
        self.assertFalse(bound["mutates_authority"])

    def test_snapshot_bound_rejects_session_or_identity_drift_before_observation(self) -> None:
        class Scope:
            def assert_ordered(self) -> None:
                pass

            def assert_context(self, _context: Mapping[str, object]) -> None:
                pass

            def hold(self) -> AbstractContextManager[object]:
                return nullcontext()

        class RejectingScope:
            def assert_ordered(self) -> None:
                pass

            def assert_context(self, _context: Mapping[str, object]) -> None:
                raise RuntimeError("stale session")

            def hold(self) -> AbstractContextManager[object]:
                return nullcontext()

        with self.assertRaisesRegex(GitAuthorityError, "trusted Git session"):
            self.adapter.snapshot_bound(
                "discover",
                CONTEXT,
                RejectingScope(),
                lease=self._lease(),
                expected_branch="master",
                expected_head="0" * 40,
            )
        observed = self.adapter.snapshot("discover", CONTEXT)
        with self.assertRaisesRegex(GitAuthorityError, "identity changed"):
            self.adapter.snapshot_bound(
                "discover",
                CONTEXT,
                Scope(),
                lease=self._lease(),
                expected_branch=str(observed["git_branch"]),
                expected_head="0" * 40,
            )

    def test_dirty_or_detached_or_mismatched_context_fails_closed(self) -> None:
        (self.root / "state").write_text("dirty\n")
        with self.assertRaisesRegex(GitAuthorityError, "not clean"):
            self.adapter.snapshot("discover", CONTEXT)
        (self.root / "state").write_text("clean\n")
        with self.assertRaisesRegex(GitAuthorityError, "mismatched"):
            self.adapter.snapshot("discover", {**CONTEXT, "backend": "sqlite"})

    def test_snapshot_bound_rejects_durable_identity_drift_before_scope(self) -> None:
        class ScopeMustNotRun:
            def assert_ordered(self) -> None:
                raise AssertionError("scope must not run")

            def assert_context(self, _context: Mapping[str, object]) -> None:
                raise AssertionError("scope must not run")

            def hold(self) -> AbstractContextManager[object]:
                raise AssertionError("scope must not run")

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
                    ScopeMustNotRun(),
                    lease=self._lease(),
                    expected_branch=str(observed["git_branch"]),
                    expected_head=str(observed["git_head"]),
                )

    def test_snapshot_bound_rejects_replaced_lease_before_backend(self) -> None:
        class ScopeMustNotRun:
            def assert_ordered(self) -> None:
                raise AssertionError("scope must not run")

            def assert_context(self, _context: Mapping[str, object]) -> None:
                raise AssertionError("scope must not run")

            def hold(self) -> AbstractContextManager[object]:
                raise AssertionError("scope must not run")

        observed = self.adapter.snapshot("discover", CONTEXT)
        replaced = AdmissionLease("project", "authority", "replacement", "owner", "barrier", 1)
        with self.assertRaisesRegex(GitAuthorityError, "session identity"):
            self.adapter.snapshot_bound(
                "discover",
                CONTEXT,
                ScopeMustNotRun(),
                lease=replaced,
                expected_branch=str(observed["git_branch"]),
                expected_head=str(observed["git_head"]),
            )

    def test_rollback_recheck_never_authorizes_mutation(self) -> None:
        result = self.adapter.verify_rollback_context(CONTEXT)
        self.assertFalse(result["rollback_context_verified"])


if __name__ == "__main__":
    unittest.main()
