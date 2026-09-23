# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile tests for the isolated SQLite authority commit capability."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.authority_neutral_commit import CommitAdmissionBundle
from tools.sqlite_authority_mutation import (
    SQLiteCommitCapability,
    SQLiteMutationAmbiguousError,
    SQLiteMutationError,
)


class SQLiteCommitCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "authority.sqlite"
        with sqlite3.connect(self.db) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE state (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("INSERT INTO state VALUES (1, 'old')")
        self._admission = CommitAdmissionBundle(
            backend="sqlite",
            target="new",
            operation_id="op-1:commit",
            fencing_token="fence-1",  # noqa: S106
            state_revision=1,
            barrier_id="barrier-1",
            artifact_identity="artifact-1",
            manifest_identity="manifest-1",
            selector_identity="selector-1",
            runtime_identity="runtime-1",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _capability(self) -> SQLiteCommitCapability:
        def identity(path: Path) -> tuple[int, int] | None:
            try:
                status = path.lstat()
            except FileNotFoundError:
                return None
            return status.st_dev, status.st_ino

        return SQLiteCommitCapability(
            self.db,
            admission=self._admission,
            expected_db_identity=identity(self.db),  # type: ignore[arg-type]
            expected_wal_identity=identity(self.db.with_name("authority.sqlite-wal")),
            expected_shm_identity=identity(self.db.with_name("authority.sqlite-shm")),
        )

    def test_commits_effect_and_verifies_integrity(self) -> None:
        def update(connection: sqlite3.Connection) -> None:
            connection.execute("UPDATE state SET value='new' WHERE id=1")

        result = self._capability().commit(update)
        self.assertEqual("ok", result.integrity_check)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(("new",), connection.execute("SELECT value FROM state").fetchone())

    def test_rejects_sidecar_identity_drift_before_effect(self) -> None:
        capability = self._capability()
        wal = self.db.with_name("authority.sqlite-wal")
        wal.unlink(missing_ok=True)

        def update(connection: sqlite3.Connection) -> None:
            connection.execute("UPDATE state SET value='bad'")

        with self.assertRaisesRegex(SQLiteMutationError, "WAL identity changed"):
            capability.commit(update)

    def test_classifies_sqlite_errors_as_ambiguous(self) -> None:
        def invalid(connection: sqlite3.Connection) -> None:
            connection.execute("UPDATE missing SET x=1")

        with self.assertRaises(SQLiteMutationAmbiguousError):
            self._capability().commit(invalid)

    def test_classifies_post_commit_identity_drift_as_ambiguous(self) -> None:
        replacement = self.root / "replacement.sqlite"

        def update(connection: sqlite3.Connection) -> None:
            connection.execute("UPDATE state SET value='new' WHERE id=1")
            shutil.copy2(self.db, replacement)
            replacement.replace(self.db)

        with self.assertRaisesRegex(
            SQLiteMutationAmbiguousError, "post-commit verification is ambiguous"
        ):
            self._capability().commit(update)


if __name__ == "__main__":
    unittest.main()
