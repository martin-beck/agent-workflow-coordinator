# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Durable SQLite execution-envelope replay and corruption coverage."""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fixture_ids import project_uuid

from tools.sqlite_storage import SQLiteBackend, StorageCorruptionError, create_database

BINDING = {
    "schema_version": 1,
    "project_id": project_uuid("9"),
    "state_repository": "owner/state",
    "product_repository": "owner/product",
}


class SQLiteExecutionEnvelopeDurabilityTests(unittest.TestCase):
    """Exercise the runtime's non-replay and exact-receipt persistence paths."""

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.tasks = self.root / "tasks"
        self.tasks.mkdir()
        self.database = self.root / "coordinator.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create(self) -> SQLiteBackend:
        task_meta = {
            "schema_version": 1,
            "id": "AR-0001",
            "title": "Test task",
            "status": "open",
            "priority": "P1",
            "summary": "Ready.",
            "next_action": "Test.",
            "task_revision": 1,
            "updated_at": "2026-10-13T00:00:00+00:00",
            "owner": "",
            "claim_expires": "",
            "worktree_key": "",
            "branch": "",
            "checkpoint_commit": "",
            "plan": "",
            "depends_on": [],
        }
        create_database(
            self.database,
            BINDING,
            [(Path("AR-0001-test.md"), task_meta, "# Test\n")],
            imported_at="2026-10-13T00:00:00+00:00",
            source_backend="git",
            source_checkpoint="a" * 40,
        )
        return SQLiteBackend(self.database, BINDING, self.tasks)

    @staticmethod
    def envelope(execution_id: str, *, task: str = "AR-0001") -> dict[str, Any]:
        return {
            "schema_version": 1,
            "execution_id": execution_id,
            "task": task,
            "task_revision": 1,
            "owner": "worker-a",
            "claim_expires": "2099-01-01T00:00:00+00:00",
            "worktree_key": "worker-a",
            "branch": "feature/worker-a",
            "spec_ref": "specs/AR-0001.json",
            "spec_revision": 1,
            "policy_digest": "a" * 64,
            "argv_sha256": "b" * 64,
            "timeout_seconds": 30,
            "created_at": "2026-10-13T00:00:00+00:00",
        }

    @staticmethod
    def result(execution_id: str, *, returncode: int = 0) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "execution_id": execution_id,
            "classification": "EXIT",
            "returncode": returncode,
            "recorded_at": "2026-10-13T00:01:00+00:00",
        }

    def test_reservation_replay_completion_and_ordering_are_exact(self) -> None:
        backend = self.create()
        first = self.envelope("11111111-1111-1111-1111-111111111111")
        second = self.envelope("22222222-2222-2222-2222-222222222222")
        second["created_at"] = "2026-10-13T00:02:00+00:00"

        first_normalized = dict(first, timeout_seconds=30.0)
        second_normalized = dict(second, timeout_seconds=30.0)
        first_result = self.result(str(first["execution_id"]))
        self.assertEqual((True, None), backend.begin_execution_envelope(first))
        self.assertEqual((False, None), backend.begin_execution_envelope(first))
        self.assertEqual(first_normalized, backend.record_execution_envelope(first))
        self.assertEqual(first_result, backend.record_execution_result(first_result))
        self.assertEqual((False, first_result), backend.begin_execution_envelope(first))
        self.assertEqual(first_result, backend.record_execution_result(first_result))
        self.assertEqual((True, None), backend.begin_execution_envelope(second))
        self.assertEqual([first_normalized, second_normalized], backend.load_execution_envelopes())
        self.assertEqual(
            [first_normalized, second_normalized], backend.load_execution_envelopes("AR-0001")
        )
        self.assertEqual([first_result], backend.load_execution_results())
        self.assertEqual([], backend.load_execution_results("AR-9999"))

    def test_conflicts_unknown_results_and_corrupt_records_fail_closed(self) -> None:
        backend = self.create()
        envelope = self.envelope("33333333-3333-3333-3333-333333333333")
        with self.assertRaisesRegex(RuntimeError, "lacks a durable intent"):
            backend.record_execution_result(self.result(envelope["execution_id"]))
        backend.record_execution_envelope(envelope)
        conflicting = dict(envelope, owner="worker-b")
        with self.assertRaisesRegex(RuntimeError, "conflicts with durable record"):
            backend.begin_execution_envelope(conflicting)
        backend.record_execution_result(self.result(envelope["execution_id"]))
        with self.assertRaisesRegex(RuntimeError, "conflicts with durable record"):
            backend.record_execution_result(self.result(envelope["execution_id"], returncode=1))

        connection = sqlite3.connect(self.database)
        connection.execute(
            "UPDATE execution_results SET result_json=? WHERE execution_id=?",
            ("[]", envelope["execution_id"]),
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(StorageCorruptionError, "invalid execution result"):
            backend.begin_execution_envelope(envelope)
        with self.assertRaisesRegex(StorageCorruptionError, "invalid execution result"):
            backend.load_execution_results()

    def test_legacy_database_has_empty_envelope_projections(self) -> None:
        backend = self.create()
        self.assertEqual([], backend.load_execution_envelopes())
        self.assertEqual([], backend.load_execution_results())

        connection = sqlite3.connect(self.database)
        connection.execute("DROP TABLE execution_envelopes")
        self.assertEqual([], backend.load_execution_envelopes())
        connection.execute(
            "CREATE TABLE execution_envelopes "
            "(envelope_json TEXT, task_id TEXT, created_at TEXT, execution_id TEXT)"
        )
        connection.execute(
            "INSERT INTO execution_envelopes VALUES (?, ?, ?, ?)",
            ("[]", "AR-0001", "2026-10-13T00:00:00+00:00", "legacy"),
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(StorageCorruptionError, "invalid execution envelope"):
            backend.load_execution_envelopes()

    def test_untrusted_execution_records_are_rejected_before_storage(self) -> None:
        backend = self.create()
        envelope = self.envelope("44444444-4444-4444-4444-444444444444")
        cases = (
            ({"unexpected": True}, "unexpected fields"),
            (dict(envelope, task="AR-invalid"), "invalid identity"),
            (dict(envelope, owner="worker\nother"), "unsafe text"),
            (dict(envelope, spec_ref=None), "incomplete spec binding"),
            (dict(envelope, policy_digest="bad"), "invalid policy_digest"),
            (dict(envelope, timeout_seconds=False), "invalid timeout"),
        )
        for candidate, expected in cases:
            with self.assertRaisesRegex(ValueError, expected):
                backend.record_execution_envelope(candidate)
        result = self.result(str(envelope["execution_id"]))
        for candidate, expected in (
            ({"unexpected": True}, "unexpected fields"),
            (dict(result, classification="UNKNOWN"), "invalid fields"),
        ):
            with self.assertRaisesRegex(ValueError, expected):
                backend.record_execution_result(candidate)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
