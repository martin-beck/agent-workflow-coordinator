# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""SQLite-only durable receipt coverage for issue-series publication."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest
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
REPOSITORY = "github.com/example/coordinator"
ISSUE_NUMBER = 42
DIGEST = "sha256:" + "a" * 64


def publication_key() -> str:
    payload = f"issue-series-publication-v1\0{REPOSITORY}\0{ISSUE_NUMBER}\0{DIGEST}"
    return hashlib.sha256(payload.encode()).hexdigest()


def task() -> tuple[Path, dict[str, Any], str]:
    return (
        Path("AR-0001-test.md"),
        {
            "schema_version": 1,
            "id": "AR-0001",
            "title": "Test",
            "status": "open",
            "priority": "P1",
            "summary": "Test.",
            "next_action": "Test.",
            "task_revision": 1,
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


class IssueSeriesPublicationSQLiteTests(unittest.TestCase):
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
        with self.backend.transaction() as connection:
            self.backend._ensure_issue_series_schema(connection)
            connection.execute(
                """INSERT INTO issue_series(
                       repository, issue_number, manifest_json, manifest_digest
                   ) VALUES (?, ?, ?, ?)""",
                (REPOSITORY, ISSUE_NUMBER, json.dumps({"schema_version": 1}), DIGEST),
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def receipt(**changes: object) -> dict[str, object]:
        result: dict[str, object] = {
            "series_identity": {"repository": REPOSITORY, "issue_number": ISSUE_NUMBER},
            "manifest_digest": DIGEST,
            "publication_key": publication_key(),
            "status": "published",
            "comment_id": "1234",
        }
        result.update(changes)
        return result

    def test_records_normalized_receipt_and_replays_without_rewriting_it(self) -> None:
        first = self.backend.record_issue_series_publication(
            self.receipt(), recorded_at="2026-10-12T01:00:00+00:00"
        )
        replay = self.backend.record_issue_series_publication(
            self.receipt(), recorded_at="2026-10-12T02:00:00+00:00"
        )
        self.assertEqual(first, replay)
        self.assertEqual([first], self.backend.load_issue_series_publications())
        connection = sqlite3.connect(self.database)
        try:
            columns = [
                row[1] for row in connection.execute("PRAGMA table_info(issue_series_publications)")
            ]
        finally:
            connection.close()
        self.assertEqual(
            [
                "repository",
                "issue_number",
                "manifest_digest",
                "publication_key",
                "status",
                "comment_id",
                "recorded_at",
            ],
            columns,
        )

    def test_conflicting_or_non_normalized_receipts_are_rejected(self) -> None:
        self.backend.record_issue_series_publication(
            self.receipt(), recorded_at="2026-10-12T01:00:00+00:00"
        )
        with self.assertRaisesRegex(RuntimeError, "conflicts with durable record"):
            self.backend.record_issue_series_publication(
                self.receipt(status="ambiguous", comment_id=None),
                recorded_at="2026-10-12T02:00:00+00:00",
            )
        with self.assertRaisesRegex(ValueError, "key disagrees"):
            self.backend.record_issue_series_publication(
                self.receipt(publication_key="b" * 64),
                recorded_at="2026-10-12T02:00:00+00:00",
            )
        with self.assertRaisesRegex(ValueError, "cannot carry a comment"):
            self.backend.record_issue_series_publication(
                self.receipt(status="deferred_timeout"),
                recorded_at="2026-10-12T02:00:00+00:00",
            )

    def test_database_import_preserves_publication_receipt(self) -> None:
        imported = self.database.with_name("imported.sqlite3")
        series = {
            "series_identity": {"repository": REPOSITORY, "issue_number": ISSUE_NUMBER},
            "manifest": {"schema_version": 1},
            "manifest_digest": DIGEST,
            "task_ids": [],
        }
        create_database(
            imported,
            BINDING,
            [task()],
            imported_at="2026-10-12T01:00:00+00:00",
            source_backend="git",
            source_checkpoint="b" * 40,
            issue_series=[series],
            issue_series_publications=[self.receipt()],
        )
        backend = SQLiteBackend(imported, BINDING, self.tasks)
        receipt = backend.load_issue_series_publications()[0]
        self.assertEqual("published", receipt["status"])
        self.assertEqual("1234", receipt["comment_id"])

    def test_legacy_schema_loads_empty_and_additive_upgrade_rolls_back(self) -> None:
        connection = sqlite3.connect(self.database, isolation_level=None)
        try:
            connection.execute("DROP TABLE issue_series_publications")
        finally:
            connection.close()
        self.assertEqual([], self.backend.load_issue_series_publications())
        upgraded = self.backend.record_issue_series_publication(
            self.receipt(), recorded_at="2026-10-12T01:00:00+00:00"
        )
        self.assertEqual([upgraded], self.backend.load_issue_series_publications())
        connection = sqlite3.connect(self.database, isolation_level=None)
        try:
            connection.execute("DROP TABLE issue_series_publications")
        finally:
            connection.close()
        connection = sqlite3.connect(self.database, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self.backend._ensure_issue_series_schema(connection)
            connection.rollback()
        finally:
            connection.close()
        check = sqlite3.connect(self.database)
        try:
            self.assertIsNone(
                check.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type='table' AND name='issue_series_publications'"""
                ).fetchone()
            )
        finally:
            check.close()


if __name__ == "__main__":
    unittest.main()
