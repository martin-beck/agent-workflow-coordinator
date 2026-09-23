# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile tests for the isolated SQLite authority commit capability."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from tools.authority_neutral_commit import CommitAdmissionBundle
from tools.sqlite_authority_mutation import (
    SQLiteCommitCapability,
    SQLiteMutationAmbiguousError,
    SQLiteMutationError,
    SQLiteMutationRejectedError,
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
        # Keep WAL/SHM identities materialized for the duration of each hostile case.
        self.keepalive = sqlite3.connect(self.db)
        self.keepalive.execute("PRAGMA wal_autocheckpoint=0")
        self.keepalive.execute("PRAGMA user_version=1")
        self.keepalive.commit()
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
        self.keepalive.close()
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
            admission_reread=lambda: self._admission.__dict__,
            expected_db_identity=identity(self.db),  # type: ignore[arg-type]
            expected_wal_identity=identity(self.db.with_name("authority.sqlite-wal")),
            expected_shm_identity=identity(self.db.with_name("authority.sqlite-shm")),
        )

    def test_commits_effect_and_verifies_integrity(self) -> None:
        def update(connection: sqlite3.Connection) -> None:
            connection.execute("UPDATE state SET value='new' WHERE id=1")

        result = self._capability().commit(update)
        self.assertEqual(
            (
                "sqlite",
                "new",
                "op-1:commit",
                1,
                "barrier-1",
                "artifact-1",
                "manifest-1",
                "selector-1",
                "runtime-1",
                "fence-1",
            ),
            (
                result.backend,
                result.target,
                result.operation_id,
                result.state_revision,
                result.barrier_id,
                result.artifact_identity,
                result.manifest_identity,
                result.selector_identity,
                result.runtime_identity,
                result.fencing_token,
            ),
        )
        self.assertEqual("ok", result.integrity_check)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(("new",), connection.execute("SELECT value FROM state").fetchone())

    def test_capability_is_single_use_but_fresh_capability_reopens(self) -> None:
        capability = self._capability()

        def first_update(connection: sqlite3.Connection) -> None:
            connection.execute("UPDATE state SET value='first'")

        capability.commit(first_update)

        called = False

        def replay(connection: sqlite3.Connection) -> None:
            nonlocal called
            called = True
            connection.execute("UPDATE state SET value='replayed'")

        with self.assertRaisesRegex(SQLiteMutationError, "already consumed"):
            capability.commit(replay)
        self.assertFalse(called)

        reopened_admission = CommitAdmissionBundle(
            backend="sqlite",
            target="new",
            operation_id="op-2:commit",
            fencing_token="fence-2",  # noqa: S106
            state_revision=2,
            barrier_id="barrier-2",
            artifact_identity="artifact-1",
            manifest_identity="manifest-1",
            selector_identity="selector-1",
            runtime_identity="runtime-1",
        )
        self._admission = reopened_admission

        def second_update(connection: sqlite3.Connection) -> None:
            connection.execute("UPDATE state SET value='second'")

        self._capability().commit(second_update)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(("second",), connection.execute("SELECT value FROM state").fetchone())

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

    def test_classifies_connection_close_failure_as_ambiguous(self) -> None:
        real_connect = sqlite3.connect

        class CloseFailingConnection:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self._connection = connection

            def __getattr__(self, name: str) -> object:
                return getattr(self._connection, name)

            def close(self) -> None:
                self._connection.close()
                raise sqlite3.OperationalError("injected close failure")

        def connector(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return cast(sqlite3.Connection, CloseFailingConnection(real_connect(*args, **kwargs)))

        def update(connection: sqlite3.Connection) -> None:
            connection.execute("UPDATE state SET value='new'")

        with self.assertRaisesRegex(
            SQLiteMutationAmbiguousError, "connection close outcome is ambiguous"
        ):
            SQLiteCommitCapability(
                self.db,
                admission=self._admission,
                admission_reread=lambda: self._admission.__dict__,
                expected_db_identity=self._capability()._db_identity,
                expected_wal_identity=self._capability()._wal_identity,
                expected_shm_identity=self._capability()._shm_identity,
                connector=connector,
            ).commit(update)

    def test_classifies_rollback_failure_as_ambiguous(self) -> None:
        real_connect = sqlite3.connect

        class RollbackFailingConnection:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self._connection = connection

            def __getattr__(self, name: str) -> object:
                return getattr(self._connection, name)

            def rollback(self) -> None:
                raise sqlite3.OperationalError("injected rollback failure")

        def connector(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return cast(
                sqlite3.Connection,
                RollbackFailingConnection(real_connect(*args, **kwargs)),
            )

        def invalid(connection: sqlite3.Connection) -> None:
            connection.execute("UPDATE missing SET x=1")

        with self.assertRaisesRegex(SQLiteMutationAmbiguousError, "rollback outcome is ambiguous"):
            capability = SQLiteCommitCapability(
                self.db,
                admission=self._admission,
                admission_reread=lambda: self._admission.__dict__,
                expected_db_identity=self._capability()._db_identity,
                expected_wal_identity=self._capability()._wal_identity,
                expected_shm_identity=self._capability()._shm_identity,
                connector=connector,
            )
            capability.commit(invalid)

    def test_rejects_stale_admission_reread_before_effect(self) -> None:
        stale = dict(self._admission.__dict__)
        stale["state_revision"] = 2
        capability = SQLiteCommitCapability(
            self.db,
            admission=self._admission,
            admission_reread=lambda: stale,
            expected_db_identity=self._capability()._db_identity,
            expected_wal_identity=self._capability()._wal_identity,
            expected_shm_identity=self._capability()._shm_identity,
        )
        called = False

        def update(connection: sqlite3.Connection) -> None:
            nonlocal called
            called = True
            connection.execute("UPDATE state SET value='bad'")

        with self.assertRaisesRegex(SQLiteMutationRejectedError, "admission identity changed"):
            capability.commit(update)
        self.assertFalse(called)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(("old",), connection.execute("SELECT value FROM state").fetchone())

    def test_rejects_every_admission_identity_drift_before_effect(self) -> None:
        admission = self._admission
        changes: dict[str, object] = {
            "backend": "git",
            "target": "rollback",
            "operation_id": "foreign-operation",
            "fencing_token": "foreign-fence",
            "state_revision": 2,
            "barrier_id": "foreign-barrier",
            "artifact_identity": "foreign-artifact",
            "manifest_identity": "foreign-manifest",
            "selector_identity": "foreign-selector",
            "runtime_identity": "foreign-runtime",
        }

        for field, value in changes.items():
            stale = dict(admission.__dict__)
            stale[field] = value
            called = False

            def update(connection: sqlite3.Connection) -> None:
                nonlocal called
                called = True
                connection.execute("UPDATE state SET value='bad'")

            def read_stale(value: dict[str, object] = stale) -> dict[str, object]:
                return value

            capability = SQLiteCommitCapability(
                self.db,
                admission=admission,
                admission_reread=read_stale,
                expected_db_identity=self._capability()._db_identity,
                expected_wal_identity=self._capability()._wal_identity,
                expected_shm_identity=self._capability()._shm_identity,
            )
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(SQLiteMutationRejectedError, "admission identity changed"),
            ):
                capability.commit(update)
            self.assertFalse(called)
            with sqlite3.connect(self.db) as connection:
                self.assertEqual(("old",), connection.execute("SELECT value FROM state").fetchone())


if __name__ == "__main__":
    unittest.main()
