# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""SQLite-only immutable execution-envelope receipt coverage."""

from __future__ import annotations

import sqlite3
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fixture_ids import project_uuid

from tools.sqlite_storage import SQLiteBackend, create_database

BINDING = {
    "schema_version": 1,
    "project_id": project_uuid("1"),
    "state_repository": "owner/state",
    "product_repository": "owner/product",
}


def task() -> tuple[Path, dict[str, Any], str]:
    return (
        Path("AR-0092-test.md"),
        {
            "schema_version": 1,
            "id": "AR-0092",
            "title": "Test",
            "status": "open",
            "priority": "P1",
            "summary": "Test.",
            "next_action": "Test.",
            "task_revision": 7,
            "updated_at": "2026-10-12T00:00:00+00:00",
            "owner": "",
            "claim_expires": "",
            "worktree_key": "",
            "branch": "",
            "checkpoint_commit": "",
            "plan": "",
            "depends_on": [],
        },
        "# Test\n",
    )


class ExecutionEnvelopeSQLiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        root = Path(self.temporary.name)
        self.database = root / "runtime" / "coordinator.sqlite3"
        self.tasks = root / "tasks"
        self.tasks.mkdir()
        create_database(
            self.database,
            BINDING,
            [task()],
            imported_at="2026-10-12T00:00:00+00:00",
            source_backend="git",
            source_checkpoint="a" * 40,
        )
        self.backend = SQLiteBackend(self.database, BINDING, self.tasks)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def envelope(**changes: object) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": 1,
            "execution_id": str(uuid.UUID(int=1)),
            "task": "AR-0092",
            "task_revision": 7,
            "owner": "worker-a",
            "claim_expires": "2026-10-12T12:00:00+00:00",
            "worktree_key": "worktree-a",
            "branch": "feature/a",
            "spec_ref": None,
            "spec_revision": None,
            "policy_digest": "a" * 64,
            "argv_sha256": "b" * 64,
            "timeout_seconds": 30,
            "created_at": "2026-10-12T11:00:00+00:00",
        }
        result.update(changes)
        return result

    @classmethod
    def outcome(cls, **changes: object) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": 1,
            "execution_id": str(uuid.UUID(int=1)),
            "classification": "EXIT",
            "returncode": 0,
            "recorded_at": "2026-10-12T11:00:01+00:00",
        }
        result.update(changes)
        return result

    def test_intent_then_result_are_immutable_and_deterministically_loaded(self) -> None:
        intent = self.backend.record_execution_envelope(self.envelope())
        self.assertEqual(intent, self.backend.record_execution_envelope(self.envelope()))
        result = self.backend.record_execution_result(self.outcome())
        self.assertEqual(result, self.backend.record_execution_result(self.outcome()))
        self.assertEqual([intent], self.backend.load_execution_envelopes("AR-0092"))
        self.assertEqual([result], self.backend.load_execution_results("AR-0092"))
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(
                ["execution_envelopes", "execution_results"],
                [
                    row[0]
                    for row in connection.execute(
                        """SELECT name FROM sqlite_master
                           WHERE type='table' AND name LIKE 'execution_%' ORDER BY name"""
                    )
                ],
            )
        finally:
            connection.close()

    def test_rejects_conflicts_unsafe_records_and_result_before_intent(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "lacks a durable intent"):
            self.backend.record_execution_result(self.outcome())
        self.backend.record_execution_envelope(self.envelope())
        with self.assertRaisesRegex(RuntimeError, "conflicts with durable record"):
            self.backend.record_execution_envelope(self.envelope(owner="worker-b"))
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            self.backend.record_execution_envelope({**self.envelope(), "argv": ["secret"]})
        self.backend.record_execution_result(self.outcome())
        with self.assertRaisesRegex(RuntimeError, "conflicts with durable record"):
            self.backend.record_execution_result(self.outcome(returncode=1))

    def test_begin_is_single_launch_or_existing_terminal_result(self) -> None:
        self.assertEqual((True, None), self.backend.begin_execution_envelope(self.envelope()))
        self.assertEqual((False, None), self.backend.begin_execution_envelope(self.envelope()))
        completed = self.backend.record_execution_result(self.outcome(returncode=9))
        self.assertEqual((False, completed), self.backend.begin_execution_envelope(self.envelope()))

    def test_import_and_legacy_additive_upgrade_preserve_atomicity(self) -> None:
        imported = self.database.with_name("imported.sqlite3")
        create_database(
            imported,
            BINDING,
            [task()],
            imported_at="2026-10-12T01:00:00+00:00",
            source_backend="git",
            source_checkpoint="b" * 40,
            execution_envelopes=[self.envelope()],
            execution_results=[self.outcome()],
        )
        backend = SQLiteBackend(imported, BINDING, self.tasks)
        self.assertEqual([self.envelope(timeout_seconds=30.0)], backend.load_execution_envelopes())
        self.assertEqual([self.outcome()], backend.load_execution_results())
        connection = sqlite3.connect(self.database, isolation_level=None)
        try:
            connection.execute("DROP TABLE execution_results")
            connection.execute("DROP TABLE execution_envelopes")
            connection.execute("BEGIN IMMEDIATE")
            self.backend._ensure_execution_envelope_schema(connection)
            connection.rollback()
        finally:
            connection.close()
        check = sqlite3.connect(self.database)
        try:
            self.assertIsNone(
                check.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type='table' AND name='execution_envelopes'"""
                ).fetchone()
            )
            self.assertIsNone(
                check.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type='table' AND name='execution_results'"""
                ).fetchone()
            )
        finally:
            check.close()


if __name__ == "__main__":
    unittest.main()
