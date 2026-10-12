# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Focused fail-closed coverage for execution-envelope handoffctl boundaries."""

from __future__ import annotations

import argparse
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from fixture_ids import project_uuid

from tools import handoffctl

CORE: Any = handoffctl


class ExecutionEnvelopeHandoffctlCoverageTest(unittest.TestCase):
    """Exercise branches that sit between envelope admission and process launch."""

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        CORE.ROOT = self.root
        CORE.TASKS = self.root / "tasks"
        CORE.RUNTIME = self.root / ".runtime"
        CORE.LOCK = CORE.RUNTIME / "state.lock"
        CORE.CONFIG = CORE.RUNTIME / "config.json"
        CORE.REPLICA_BLOCKED = CORE.RUNTIME / "replica-blocked.json"
        CORE.PROJECT_CONFIG = self.root / ".handoffctl.json"
        CORE.BINDING = self.root / "coordinator.binding.json"
        CORE.BACKEND_CONFIG = self.root / "coordinator.backend.json"
        CORE.DATABASE = CORE.RUNTIME / "coordinator.sqlite3"
        CORE.TASKS.mkdir()
        (self.root / "plans").mkdir()
        CORE.PROJECT_CONFIG.write_text(
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
        CORE.BINDING.write_text(
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

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def configure_product_invocation(self, product: Path) -> None:
        CORE.CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CORE.CONFIG.write_text(
            json.dumps(
                {
                    "projects_root": str(product.parent),
                    "product_worktree": product.name,
                    "github_repository": "owner/product",
                    "push_enabled": False,
                }
            ),
            encoding="utf-8",
        )

    def test_invocation_worktree_detects_nested_detached_checkout_and_fences_it(self) -> None:
        """A nested product checkout is identified and must match the declared branch."""
        product = self.root / "projects" / "product"
        nested = product / "nested"
        nested.mkdir(parents=True)
        self.configure_product_invocation(product)

        def detached_git(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[-2:] == ["rev-parse", "--show-toplevel"]:
                return subprocess.CompletedProcess(args, 0, str(product) + "\n", "")
            if args[-4:] == ["symbolic-ref", "--short", "-q", "HEAD"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            raise AssertionError(args)

        with (
            patch.object(CORE.Path, "cwd", return_value=nested),
            patch.object(CORE, "run", side_effect=detached_git),
        ):
            self.assertEqual(("product", "DETACHED"), CORE.invocation_worktree())
            with self.assertRaisesRegex(RuntimeError, "lacks declared worktree"):
                CORE.assert_invocation_worktree({"id": "AR-0001"})
            with self.assertRaisesRegex(RuntimeError, "branch 'DETACHED'"):
                CORE.assert_invocation_worktree(
                    {"id": "AR-0001", "worktree_key": "product", "branch": "feature/live"}
                )

    def test_sandbox_builds_only_present_runtime_mounts_and_preserves_mount_modes(self) -> None:
        """A private sandbox delegates all source mounts to descriptor binding."""
        base = CORE.sandbox_base_command()
        self.assertNotIn("--ro-bind", base)
        self.assertIn("--unshare-net", base)
        self.assertIn("--clearenv", base)

        root = self.root / "sandbox"
        (root / "read").mkdir(parents=True)
        (root / "write").mkdir()
        args = argparse.Namespace(
            command=["tool", "--check"],
            _envelope_worktree=str(root),
            _envelope_read_scope=["read"],
            _envelope_write_scope=["write"],
        )
        with patch.object(CORE.execution_envelope.shutil, "which", return_value="/usr/bin/bwrap"):
            command = CORE.sandboxed_envelope_command(args)
        self.assertEqual(
            str(Path(CORE.execution_envelope.__file__).resolve().parent.parent), command[3]
        )
        payload = json.loads(command[4])
        self.assertEqual(str(root), payload["specification"]["root"])
        self.assertEqual(
            {
                "relative": "read",
                "destination": "/worktree/read",
                "mode": "--ro-bind",
            },
            payload["specification"]["mounts"][0],
        )
        self.assertEqual(["--", "tool", "--check"], command[-3:])

    def test_reservation_mutation_failure_prevents_bound_subprocess_launch(self) -> None:
        """No process starts unless the durable reservation mutation has succeeded."""
        args = argparse.Namespace(
            task="AR-0001",
            owner="worker-a",
            command=["would-not-run"],
            command_class="test",
            scope_path=[],
            timeout_seconds=10,
            _envelope_worktree=str(self.root / "product"),
        )
        current = {"task_revision": 7}
        with (
            patch.object(CORE, "backend_selection", return_value={"backend": "sqlite"}),
            patch.object(CORE, "require_active_owner", return_value=CORE.DEFAULT_EVIDENCE_POLICY),
            patch.object(CORE, "run_envelope_timeout", return_value=20),
            patch.object(CORE, "sandboxed_envelope_command", return_value=["would-not-run"]),
            patch.object(CORE, "locate", return_value=(self.root / "task", current, "")),
            patch.object(CORE, "mutate", side_effect=RuntimeError("reservation rejected")),
            patch.object(CORE.subprocess, "run") as launched,
            self.assertRaisesRegex(RuntimeError, "reservation rejected"),
        ):
            CORE._cmd_run_fenced(args)
        launched.assert_not_called()

    def test_completion_failure_preserves_durable_result_and_skips_reconciliation(self) -> None:
        """A recorded result is retained when exact reservation completion is rejected."""
        args = argparse.Namespace(task="AR-0001", owner="worker-a", command=["true"])
        policy = CORE.DEFAULT_EVIDENCE_POLICY
        with (
            patch.object(
                CORE,
                "_cmd_run_fenced",
                return_value=(policy, "a" * 64, 5.0, False, 0, "b" * 32, 8),
            ),
            patch.object(CORE, "append_command_result") as recorded,
            patch.object(CORE, "mutate", side_effect=[None, RuntimeError("stale reservation")]) as finish,
            patch.object(CORE, "locate", return_value=(self.root / "task", {"task_revision": 9}, "")),
            patch.object(CORE, "reconcile") as reconciled,
            self.assertRaisesRegex(RuntimeError, "stale reservation"),
        ):
            CORE.cmd_run(args)
        recorded.assert_called_once_with("AR-0001", "worker-a", "a" * 64, 0, False)
        self.assertEqual(["execution-result", "execution-finish"], [call.args[1] for call in finish.call_args_list])
        reconciled.assert_not_called()

    def test_journal_failure_keeps_bound_execution_fence_and_skips_completion(self) -> None:
        """An uncertain command result cannot clear its exact durable execution fence."""
        args = argparse.Namespace(task="AR-0001", owner="worker-a", command=["true"])
        policy = CORE.DEFAULT_EVIDENCE_POLICY
        with (
            patch.object(
                CORE,
                "_cmd_run_fenced",
                return_value=(policy, "a" * 64, 5.0, False, 0, "b" * 32, 8),
            ),
            patch.object(CORE, "append_command_result", side_effect=OSError("disk full")),
            patch.object(CORE, "mutate") as completion,
            patch.object(CORE, "locate", return_value=(self.root / "task", {"task_revision": 9}, "")),
            patch.object(CORE, "reconcile") as reconciled,
            self.assertRaisesRegex(RuntimeError, "EXECUTION_RESULT_UNKNOWN"),
        ):
            CORE.cmd_run(args)
        completion.assert_called_once()
        self.assertEqual("execution-result", completion.call_args.args[1])
        reconciled.assert_not_called()

    def test_recovery_requires_expired_reservation_before_worker_probe(self) -> None:
        """A live reservation rejects recovery without probing or clearing its fence."""
        reservation = {
            "execution_id": "a" * 32,
            "owner": "worker-a",
            "task_revision": 4,
            "envelope_digest": "sha256:" + "b" * 64,
            "command_class": "test",
            "scope_digest": "sha256:" + "c" * 64,
            "argv_digest": "d" * 64,
            "expires_at": "2999-01-01T00:00:00+00:00",
        }
        meta: dict[str, object] = {"task_revision": 4, "execution_reservation": reservation}
        recover = argparse.Namespace(expected_revision=4, note="recover")
        with (
            patch.object(CORE, "require_claimed_worker_absent") as probe,
            self.assertRaisesRegex(RuntimeError, "EXECUTION_IN_PROGRESS"),
        ):
            CORE.apply_recover_execution(recover, meta)
        probe.assert_not_called()
        self.assertIs(meta["execution_reservation"], reservation)


def _mounts(command: list[str]) -> list[list[str]]:
    """Extract complete Bubblewrap mount option groups for exact assertions."""
    return [
        command[index : index + 3]
        for index, value in enumerate(command)
        if value in {"--bind", "--ro-bind"}
    ]


if __name__ == "__main__":
    unittest.main()
