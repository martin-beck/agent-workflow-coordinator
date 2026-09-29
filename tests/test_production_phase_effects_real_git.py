# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

# Fixed Git test executable and arguments are intentional fixtures.
# ruff: noqa: S603, S607

from __future__ import annotations

import subprocess
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path

from tools.authority_neutral_commit import CommitAdmissionBundle
from tools.git_authority_adapter import GitAuthorityAdapter
from tools.rollback_control_store import SQLiteBarrierSessionStore, SQLiteRollbackControlStore
from tools.upgrade_identity import (
    BarrierChildIdentity,
    BarrierSessionIdentity,
    canonical_barrier_session_digest,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _admission() -> CommitAdmissionBundle:
    return CommitAdmissionBundle(
        backend="git",
        target="new",
        operation_id="real-git-test:commit",
        fencing_token="fence-real",  # noqa: S106
        state_revision=3,
        barrier_id="barrier-real",
        artifact_identity="artifact-real",
        manifest_identity="manifest-real",
        selector_identity="selector-real",
        runtime_identity="runtime-real",
    )


class RealGitPhaseEffectsTests(unittest.TestCase):
    def test_real_git_durable_commit_round_trip_and_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            control_path = root.parent / "control.sqlite"
            _git(root, "init", "-b", "main")
            _git(root, "config", "user.name", "Test Runner")
            _git(root, "config", "user.email", "test@example.invalid")
            (root / "state").write_text("old\n", encoding="utf-8")
            _git(root, "add", "state")
            _git(root, "commit", "-m", "initial")
            expected_head = _git(root, "rev-parse", "HEAD")

            project = str(uuid.uuid4())
            identity_record: dict[str, object] = {
                "schema_version": 1,
                "project_id": project,
                "attempt_id": "real-git-attempt",
                "state_revision": 1,
                "authority_revision_at_acquire": "authority-real",
                "durable_barrier_id": "barrier-real",
                "fencing_token": "fence-real",
                "fencing_owner": "owner-real",
                "identity_digest": "0" * 64,
            }
            identity_record["identity_digest"] = canonical_barrier_session_digest(identity_record)
            identity = BarrierSessionIdentity.from_record(identity_record)
            control = SQLiteRollbackControlStore(control_path, project)
            session = SQLiteBarrierSessionStore(control, lambda: "authority-real")
            session.create(identity)
            session.bind_child(
                1,
                BarrierChildIdentity.bind(identity, "real-git-test:commit", "new"),
            )
            session.bind_child(
                2,
                BarrierChildIdentity.bind(identity, "real-git-test:rollback", "rollback"),
            )
            admission = _admission()
            adapter = GitAuthorityAdapter(root)

            def reread() -> dict[str, object]:
                state = session.snapshot()
                if state is None:
                    raise AssertionError("durable session disappeared")
                return {
                    "backend": "git",
                    "target": "new",
                    "operation_id": admission.operation_id,
                    "state_revision": state.revision,
                    "barrier_id": state.identity.durable_barrier_id,
                    "fencing_token": state.identity.fencing_token,
                    "artifact_identity": admission.artifact_identity,
                    "manifest_identity": admission.manifest_identity,
                    "selector_identity": admission.selector_identity,
                    "runtime_identity": admission.runtime_identity,
                }

            capability = adapter.bind_durable_commit_capability(
                admission,
                session,
                session_revision=3,
                admission_reread=reread,
                expected_branch="main",
                expected_head=expected_head,
            )
            (root / "state").write_text("new\n", encoding="utf-8")
            _git(root, "add", "state")
            receipt = capability.execute("real-git-test authority commit")

            self.assertTrue(receipt.mutates_authority)
            self.assertNotEqual(expected_head, _git(root, "rev-parse", "HEAD"))
            self.assertEqual("", _git(root, "status", "--porcelain=v1", "--untracked-files=all"))
            reopened = SQLiteRollbackControlStore(control.path, project)
            reopened_state = SQLiteBarrierSessionStore(reopened).snapshot()
            self.assertIsNotNone(reopened_state)
            assert reopened_state is not None
            self.assertEqual("held", reopened_state.status)
            with closing(__import__("sqlite3").connect(control.path)) as check:
                outcomes = check.execute(
                    "SELECT operation_id, outcome FROM authority_effect_intent WHERE project_id=?",
                    (project,),
                ).fetchall()
            self.assertEqual([("real-git-test:commit", "committed")], outcomes)


if __name__ == "__main__":
    unittest.main()
