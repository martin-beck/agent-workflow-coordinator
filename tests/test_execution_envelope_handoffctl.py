# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""End-to-end Git handoffctl execution-envelope coverage."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_handoffctl

CORE = test_handoffctl.CORE


class ExecutionEnvelopeHandoffTests(unittest.TestCase):
    setUp = test_handoffctl.HandoffTest.setUp
    tearDown = test_handoffctl.HandoffTest.tearDown
    make_task = test_handoffctl.HandoffTest.make_task
    refresh_views = test_handoffctl.HandoffTest.refresh_views

    def test_run_envelope_fsyncs_intent_before_launch_and_pairs_result(self) -> None:
        self.make_task(
            status="in_progress",
            owner="worker-a",
            claim_expires="2099-01-01T00:00:00+00:00",
            worktree_key="checkout-a",
            branch="feature/a",
        )
        CORE.CONFIG.parent.mkdir(exist_ok=True)
        CORE.CONFIG.write_text("{}")
        intent = {
            "schema_version": 1,
            "execution_id": "00000000-0000-0000-0000-000000000001",
            "task": "AR-0001",
            "task_revision": 1,
            "owner": "worker-a",
            "claim_expires": "2099-01-01T00:00:00+00:00",
            "worktree_key": "checkout-a",
            "branch": "feature/a",
            "spec_ref": None,
            "spec_revision": None,
            "policy_digest": hashlib.sha256(b"absent").hexdigest(),
            "argv_sha256": CORE.argv_digest(["true"]),
            "timeout_seconds": 10.0,
            "created_at": "2026-10-12T12:00:00+00:00",
        }
        path = self.root / ".runtime/intents/launch.json"
        path.parent.mkdir()
        path.write_text(json.dumps(intent))
        args = argparse.Namespace(
            task="AR-0001",
            owner="worker-a",
            timeout_seconds=10.0,
            envelope=".runtime/intents/launch.json",
            command=["true"],
        )
        completed = subprocess.CompletedProcess(["true"], 0, stdout="", stderr="")
        with (
            patch.object(CORE.subprocess, "run", return_value=completed) as command,
            patch.object(CORE, "mutate"),
            patch.object(CORE, "reconcile"),
        ):
            self.assertEqual(0, CORE.cmd_run(args))
        command.assert_called_once_with(
            ["true"], check=False, stdin=subprocess.DEVNULL, timeout=10.0
        )
        records = [
            json.loads(line)
            for line in (self.root / "execution-envelopes/AR-0001.jsonl").read_text().splitlines()
        ]
        self.assertEqual(intent, records[0])
        self.assertEqual(("EXIT", 0), (records[1]["classification"], records[1]["returncode"]))

    def test_run_envelope_rejects_mismatched_command_without_launch(self) -> None:
        self.make_task(
            status="in_progress",
            owner="worker-a",
            claim_expires="2099-01-01T00:00:00+00:00",
            worktree_key="checkout-a",
            branch="feature/a",
        )
        CORE.CONFIG.parent.mkdir(exist_ok=True)
        CORE.CONFIG.write_text("{}")
        path = self.root / ".runtime/intent.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "execution_id": "00000000-0000-0000-0000-000000000001",
                    "task": "AR-0001",
                    "task_revision": 1,
                    "owner": "worker-a",
                    "claim_expires": "2099-01-01T00:00:00+00:00",
                    "worktree_key": "checkout-a",
                    "branch": "feature/a",
                    "spec_ref": None,
                    "spec_revision": None,
                    "policy_digest": hashlib.sha256(b"absent").hexdigest(),
                    "argv_sha256": CORE.argv_digest(["not-true"]),
                    "timeout_seconds": 10.0,
                    "created_at": "2026-10-12T12:00:00+00:00",
                }
            )
        )
        with (
            patch.object(CORE.subprocess, "run") as command,
            self.assertRaisesRegex(RuntimeError, "does not match"),
        ):
            CORE.cmd_run(
                argparse.Namespace(
                    task="AR-0001",
                    owner="worker-a",
                    timeout_seconds=10.0,
                    envelope=".runtime/intent.json",
                    command=["true"],
                )
            )
        command.assert_not_called()

    def test_git_execution_receipt_appends_serialize_concurrent_workers(self) -> None:
        def intent(index: int) -> dict[str, object]:
            return {
                "schema_version": 1,
                "execution_id": f"00000000-0000-0000-0000-{index:012d}",
                "task": "AR-0001",
                "task_revision": 1,
                "owner": "worker-a",
                "claim_expires": "2099-01-01T00:00:00+00:00",
                "worktree_key": "checkout-a",
                "branch": "feature/a",
                "spec_ref": None,
                "spec_revision": None,
                "policy_digest": "a" * 64,
                "argv_sha256": "b" * 64,
                "timeout_seconds": 10.0,
                "created_at": "2026-10-12T12:00:00+00:00",
            }

        with ThreadPoolExecutor(max_workers=16) as workers:
            futures = [
                workers.submit(CORE.append_execution_intent, "AR-0001", intent(index))
                for index in range(1, 17)
            ]
            for future in futures:
                future.result()
        records = CORE.load_execution_records(self.root, "AR-0001")
        self.assertEqual(16, len(records))
        self.assertEqual(
            {f"00000000-0000-0000-0000-{index:012d}" for index in range(1, 17)},
            {record["execution_id"] for record in records},
        )

    def test_git_execution_intent_replay_never_relaunches_and_rejects_cross_task_result(
        self,
    ) -> None:
        self.make_task("AR-0001")
        self.make_task("AR-0002")
        intent = {
            "schema_version": 1,
            "execution_id": "00000000-0000-0000-0000-000000000001",
            "task": "AR-0001",
            "task_revision": 1,
            "owner": "worker-a",
            "claim_expires": "2099-01-01T00:00:00+00:00",
            "worktree_key": "checkout-a",
            "branch": "feature/a",
            "spec_ref": None,
            "spec_revision": None,
            "policy_digest": "a" * 64,
            "argv_sha256": "b" * 64,
            "timeout_seconds": 10.0,
            "created_at": "2026-10-12T12:00:00+00:00",
        }
        self.assertIsNone(CORE.append_execution_intent("AR-0001", intent))
        with self.assertRaisesRegex(RuntimeError, "another task history"):
            CORE.append_execution_intent("AR-0002", {**intent, "task": "AR-0002"})
        with self.assertRaisesRegex(RuntimeError, "EXECUTION_UNCERTAIN"):
            CORE.append_execution_intent("AR-0001", intent)
        result = {
            "schema_version": 1,
            "execution_id": intent["execution_id"],
            "classification": "EXIT",
            "returncode": 7,
            "recorded_at": "2026-10-12T12:01:00+00:00",
        }
        CORE.append_execution_result("AR-0001", result)
        self.assertEqual(result, CORE.append_execution_intent("AR-0001", intent))
        CORE.append_execution_record(self.root, "AR-0002", result)
        self.assertIn("same-history", "\n".join(CORE.validate()))
