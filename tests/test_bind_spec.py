# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Hostile lifecycle coverage for legacy task-spec binding."""

import argparse
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from fixture_ids import project_uuid

from tools import handoffctl as core

ROOT = Path(__file__).resolve().parents[1]


class BindSpecTests(unittest.TestCase):
    """Exercise the lifecycle transition in an isolated state root."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = cast(Any, core)
        self.originals = {
            name: getattr(core, name)
            for name in (
                "ROOT",
                "TASKS",
                "RUNTIME",
                "LOCK",
                "CONFIG",
                "REPLICA_BLOCKED",
                "PROJECT_CONFIG",
                "BINDING",
                "BACKEND_CONFIG",
                "DATABASE",
            )
        }
        core.ROOT = self.root
        core.TASKS = self.root / "tasks"
        core.RUNTIME = self.root / ".runtime"
        core.LOCK = core.RUNTIME / "state.lock"
        core.CONFIG = core.RUNTIME / "config.json"
        core.REPLICA_BLOCKED = core.RUNTIME / "replica-blocked.json"
        core.PROJECT_CONFIG = self.root / ".handoffctl.json"
        core.BINDING = self.root / "coordinator.binding.json"
        core.BACKEND_CONFIG = self.root / "coordinator.backend.json"
        core.DATABASE = core.RUNTIME / "coordinator.sqlite3"
        core.TASKS.mkdir()
        (self.root / "plans").mkdir()
        core.PROJECT_CONFIG.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project_id": project_uuid("1"),
                    "project_name": "test-project",
                    "project_title": "Test Project",
                    "status_view": True,
                    "commit_signoff": True,
                }
            ),
            encoding="utf-8",
        )
        core.BINDING.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project_id": project_uuid("1"),
                    "state_repository": "owner/state",
                    "product_repository": "owner/product",
                }
            ),
            encoding="utf-8",
        )
        core.RUNTIME.mkdir()
        core.CONFIG.write_text(
            json.dumps(
                {
                    "projects_root": str(self.root.parent),
                    "product_worktree": "product",
                    "github_repository": "owner/product",
                    "push_enabled": False,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        for name, value in self.originals.items():
            setattr(core, name, value)
        self.temp.cleanup()

    def make_task(self) -> None:
        meta = {
            "schema_version": 1,
            "id": "AR-0001",
            "title": "Test task",
            "status": "in_progress",
            "priority": "P1",
            "summary": "Ready for testing.",
            "next_action": "Run the test.",
            "task_revision": 1,
            "updated_at": "2026-09-03T20:00:00+00:00",
            "owner": "worker-a",
            "claim_expires": "2099-01-01T00:00:00+00:00",
            "worktree_key": "",
            "branch": "",
            "checkpoint_commit": "",
            "plan": "",
            "depends_on": [],
        }
        core.write_task(core.TASKS / "AR-0001-test.md", meta, "# Test\n")
        tasks = core.all_tasks()
        core.atomic(self.root / "CURRENT.md", core.render_current(tasks))
        core.atomic(self.root / "STATUS.md", core.render_status_view(tasks))

    def write_policy_and_spec(self) -> None:
        (self.root / "task-spec-policy.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "additional_evidence_classes": [
                        "hosted",
                        "offline",
                        "privacy",
                        "journey",
                        "quality",
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        subprocess.run(["/usr/bin/git", "init", "-q", str(self.root)], check=True)  # noqa: S603
        subprocess.run(  # noqa: S603
            ["/usr/bin/git", "-C", str(self.root), "add", "task-spec-policy.json"], check=True
        )
        subprocess.run(  # noqa: S603
            [
                "/usr/bin/git",
                "-C",
                str(self.root),
                "-c",
                "user.name=Policy Test",
                "-c",
                "user.email=policy@example.invalid",
                "commit",
                "-qm",
                "policy fixture",
            ],
            check=True,
        )
        spec = json.loads((ROOT / "examples/task-specs/downstream-additive.json").read_text())
        spec["spec_ref"] = "spec.json"
        (self.root / "spec.json").write_text(json.dumps(spec) + "\n", encoding="utf-8")

    def test_bind_spec_fences_legacy_active_task_and_validates_spec(self) -> None:
        self.write_policy_and_spec()
        self.make_task()
        args = argparse.Namespace(
            task="AR-0001",
            owner="worker-a",
            expected_revision=1,
            spec_ref="spec.json",
            spec_revision=1,
        )
        with (
            patch.object(core, "sync_replica_before_write"),
            patch.object(core, "commit", return_value=True),
            patch.object(core, "push_replica"),
        ):
            for replacement, message in (
                ({"owner": "other"}, "owned by worker-a"),
                ({"expected_revision": 2}, "stale revision"),
                ({"spec_ref": "../bad.json"}, "task spec is invalid"),
                ({"spec_revision": 2}, "task spec is invalid"),
            ):
                with (
                    self.subTest(replacement=replacement),
                    self.assertRaisesRegex(RuntimeError, message),
                ):
                    core.mutate(argparse.Namespace(**{**vars(args), **replacement}), "bind-spec")
                self.assertNotIn("spec_ref", core.locate("AR-0001")[1])
            expired = {**core.locate("AR-0001")[1], "claim_expires": "2000-01-01T00:00:00+00:00"}
            with self.assertRaisesRegex(RuntimeError, "unexpired owner lease"):
                core.apply_bind_spec(args, expired, self.runtime.DEFAULT_EVIDENCE_POLICY)
            core.mutate(args, "bind-spec")
        bound = core.locate("AR-0001")[1]
        self.assertEqual("spec.json", bound["spec_ref"])
        self.assertEqual(1, bound["spec_revision"])
        self.assertEqual(2, bound["task_revision"])
        with self.assertRaisesRegex(RuntimeError, "requires an unbound task spec"):
            core.apply_bind_spec(
                argparse.Namespace(**{**vars(args), "expected_revision": 2}),
                bound,
                self.runtime.DEFAULT_EVIDENCE_POLICY,
            )

    def test_bind_worktree_fences_legacy_active_identity(self) -> None:
        self.make_task()
        args = argparse.Namespace(
            task="AR-0001",
            owner="worker-a",
            expected_revision=1,
            branch="repair/ar-0001",
            worktree_key="worker-one",
        )
        with (
            patch.object(core, "sync_replica_before_write"),
            patch.object(core, "commit", return_value=True),
            patch.object(core, "push_replica"),
        ):
            for replacement, message in (
                ({"owner": "other"}, "owned by worker-a"),
                ({"expected_revision": 2}, "stale revision"),
                ({"branch": "DETACHED"}, "branch is invalid"),
                ({"branch": "HEAD"}, "branch is invalid"),
                ({"branch": "feature//x"}, "branch is invalid"),
                ({"branch": "feature.lock"}, "branch is invalid"),
                ({"branch": "feature/.tmp"}, "branch is invalid"),
                ({"branch": "@{-1}"}, "branch is invalid"),
                ({"worktree_key": "../escape"}, "worktree key is invalid"),
            ):
                with (
                    self.subTest(replacement=replacement),
                    self.assertRaisesRegex(RuntimeError, message),
                ):
                    core.mutate(
                        argparse.Namespace(**{**vars(args), **replacement}), "bind-worktree"
                    )
                current = core.locate("AR-0001")[1]
                self.assertEqual("", current["branch"])
                self.assertEqual("", current["worktree_key"])
            with patch.object(
                core,
                "run",
                return_value=subprocess.CompletedProcess(
                    ["git", "check-ref-format", "--branch", "@{-1}"], 0, "repair/previous\n", ""
                ),
            ):
                self.assertFalse(core.valid_binding_branch("@{-1}"))
            expired = {**core.locate("AR-0001")[1], "claim_expires": "2000-01-01T00:00:00+00:00"}
            with self.assertRaisesRegex(RuntimeError, "unexpired owner lease"):
                core.apply_bind_worktree(args, expired, core.all_tasks())
            core.mutate(args, "bind-worktree")
        bound = core.locate("AR-0001")[1]
        self.assertEqual("repair/ar-0001", bound["branch"])
        self.assertEqual("worker-one", bound["worktree_key"])
        revision = bound["task_revision"]
        body = core.locate("AR-0001")[2]
        with (
            patch.object(core, "sync_replica_before_write"),
            patch.object(core, "commit", return_value=True) as commit,
            patch.object(core, "push_replica"),
        ):
            core.mutate(
                argparse.Namespace(**{**vars(args), "expected_revision": revision}),
                "bind-worktree",
            )
        replayed = core.locate("AR-0001")
        self.assertEqual(revision, replayed[1]["task_revision"])
        self.assertEqual(body, replayed[2])
        commit.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "empty legacy worktree metadata"):
            core.apply_bind_worktree(
                argparse.Namespace(
                    **{**vars(args), "expected_revision": revision, "worktree_key": "other-worker"}
                ),
                bound,
                core.all_tasks(),
            )

        partial_args = argparse.Namespace(**{**vars(args), "expected_revision": revision})
        partial_key = {**bound, "branch": "", "worktree_key": args.worktree_key}
        core.apply_bind_worktree(partial_args, partial_key, core.all_tasks())
        self.assertEqual(
            (args.branch, args.worktree_key),
            (partial_key["branch"], partial_key["worktree_key"]),
        )
        with self.assertRaisesRegex(RuntimeError, "worktree key conflicts"):
            core.apply_bind_worktree(
                argparse.Namespace(**{**vars(partial_args), "worktree_key": "other-worker"}),
                {**bound, "branch": "", "worktree_key": args.worktree_key},
                core.all_tasks(),
            )

        persisted_input = {**bound, "branch": "", "worktree_key": args.worktree_key}
        core.write_task(core.TASKS / "AR-0001-test.md", persisted_input, body)
        with (
            patch.object(core, "sync_replica_before_write"),
            patch.object(core, "commit", return_value=True),
            patch.object(core, "push_replica"),
        ):
            core.mutate(partial_args, "bind-worktree")
        persisted_partial = core.locate("AR-0001")[1]
        self.assertEqual(
            (args.branch, args.worktree_key, revision + 1),
            (
                persisted_partial["branch"],
                persisted_partial["worktree_key"],
                persisted_partial["task_revision"],
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "requires an empty legacy branch"):
            core.apply_bind_worktree(
                partial_args,
                {**bound, "branch": args.branch, "worktree_key": ""},
                core.all_tasks(),
            )

    def test_bind_worktree_validation_fails_closed_and_rejects_conflicts(self) -> None:
        self.make_task()
        args = argparse.Namespace(
            task="AR-0001",
            owner="worker-a",
            expected_revision=1,
            branch="repair/ar-0001",
            worktree_key="worker-one",
        )
        self.assertFalse(core.valid_binding_branch(None))
        self.assertFalse(core.valid_binding_branch("x" * 129))
        self.assertFalse(core.valid_binding_branch("DETACHED"))
        with patch.object(
            core,
            "run",
            return_value=subprocess.CompletedProcess(
                ["git", "check-ref-format", "--branch", "bad"], 1, "", "invalid"
            ),
        ):
            self.assertFalse(core.valid_binding_branch("bad"))
        with patch.object(core, "run", side_effect=core.SubprocessTimeoutError("timeout")):
            self.assertFalse(core.valid_binding_branch("repair/ar-0001"))
        self.assertTrue(core.valid_binding_worktree("worker-one"))
        self.assertFalse(core.valid_binding_worktree(None))
        self.assertFalse(core.valid_binding_worktree("product"))
        self.assertFalse(core.valid_binding_worktree("../escape"))
        self.assertFalse(core.valid_binding_worktree("/outside/escape"))
        self.assertFalse(core.valid_binding_worktree("nested/worker"))
        meta = core.locate("AR-0001")[1]
        invalid_metadata = {**meta, "worktree_key": "../escape"}
        self.assertIn(
            "AR-0001: invalid worktree_key",
            core.value_errors(core.TASKS / "AR-0001-test.md", invalid_metadata),
        )
        reserved_metadata = {**meta, "worktree_key": "product"}
        self.assertIn(
            "AR-0001: invalid worktree_key",
            core.value_errors(core.TASKS / "AR-0001-test.md", reserved_metadata),
        )
        conflict = {
            **meta,
            "id": "AR-0002",
            "branch": args.branch,
            "worktree_key": "other-worker",
        }
        self.assertTrue(
            core.binding_identity_conflicts(args, meta, [(self.root, conflict, "# Test")])
        )
        with self.assertRaisesRegex(RuntimeError, "identity conflicts"):
            core.require_bind_worktree_admission(args, meta, [(self.root, conflict, "# Test")])
        inactive = {**meta, "status": "ready"}
        with self.assertRaisesRegex(RuntimeError, "requires an active task"):
            core.require_active_binding_owner(args, inactive)
        malformed_expiry = {**meta, "claim_expires": "not-a-date"}
        with self.assertRaisesRegex(RuntimeError, "unexpired owner lease"):
            core.require_active_binding_owner(args, malformed_expiry)
