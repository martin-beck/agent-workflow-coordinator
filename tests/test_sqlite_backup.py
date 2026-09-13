# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Negative-path tests for online SQLite backup and restore."""

from __future__ import annotations

import importlib.util
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sqlite_backup", ROOT / "tools/sqlite_backup.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
BackupError = MODULE.BackupError
backup_database = MODULE.backup_database
restore_database = MODULE.restore_database
write_manifest = MODULE.write_manifest

BINDING = {
    "project_id": "11111111-1111-4111-8111-111111111111",
    "state_repository": "owner/state",
    "product_repository": "owner/product",
}


def create_database(path: Path, *, body: str = "before") -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE records(id INTEGER PRIMARY KEY, body TEXT NOT NULL);
        """
    )
    connection.executemany(
        "INSERT INTO metadata VALUES (?, ?)",
        [
            ("schema_version", "1"),
            ("backend", "sqlite"),
            ("project_id", BINDING["project_id"]),
            ("state_repository", BINDING["state_repository"]),
            ("product_repository", BINDING["product_repository"]),
            ("state", "active"),
        ],
    )
    connection.execute("INSERT INTO records(body) VALUES (?)", (body,))
    connection.commit()
    connection.close()


class SQLiteBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.sqlite3"
        create_database(self.source)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_online_backup_manifest_and_restore_round_trip(self) -> None:
        backup = self.root / "backup.sqlite3"
        manifest = backup_database(self.source, backup, BINDING)
        self.assertTrue(manifest["wal_consistent"])
        destination = self.root / "restored.sqlite3"
        restore_database(backup, destination, manifest, BINDING, quiesced=True)
        with sqlite3.connect(destination) as connection:
            self.assertEqual("before", connection.execute("SELECT body FROM records").fetchone()[0])
        manifest_path = self.root / "backup-manifest.json"
        write_manifest(manifest_path, manifest)
        self.assertIn('"kind": "sqlite-online-backup"', manifest_path.read_text())

    def test_restore_requires_quiescence_and_refuses_existing_backup(self) -> None:
        backup = self.root / "backup.sqlite3"
        manifest = backup_database(self.source, backup, BINDING)
        with self.assertRaises(BackupError):
            restore_database(
                backup, self.root / "restored.sqlite3", manifest, BINDING, quiesced=False
            )
        with self.assertRaises(BackupError):
            backup_database(self.source, backup, BINDING)

    def test_restore_rejects_live_destination_sidecars(self) -> None:
        backup = self.root / "backup.sqlite3"
        manifest = backup_database(self.source, backup, BINDING)
        (self.root / "restored.sqlite3-wal").write_bytes(b"live")
        with self.assertRaises(BackupError):
            restore_database(
                backup, self.root / "restored.sqlite3", manifest, BINDING, quiesced=True
            )

    def test_manifest_unknown_fields_fail_closed(self) -> None:
        backup = self.root / "backup.sqlite3"
        manifest = backup_database(self.source, backup, BINDING)
        manifest["extra"] = True
        with self.assertRaises(BackupError):
            restore_database(
                backup, self.root / "restored.sqlite3", manifest, BINDING, quiesced=True
            )

    def test_manifest_tampering_and_corruption_fail_closed(self) -> None:
        backup = self.root / "backup.sqlite3"
        manifest = backup_database(self.source, backup, BINDING)
        manifest["database_sha256"] = "0" * 64
        with self.assertRaises(BackupError):
            restore_database(
                backup, self.root / "restored.sqlite3", manifest, BINDING, quiesced=True
            )
        backup.write_bytes(b"not sqlite")
        with self.assertRaises(BackupError):
            restore_database(
                backup, self.root / "restored.sqlite3", manifest, BINDING, quiesced=True
            )

    def test_binding_mismatch_fails_before_backup(self) -> None:
        wrong = dict(BINDING, project_id="22222222-2222-4222-8222-222222222222")
        with self.assertRaises(BackupError):
            backup_database(self.source, self.root / "backup.sqlite3", wrong)

    def test_source_symlink_is_rejected(self) -> None:
        link = self.root / "source-link.sqlite3"
        link.symlink_to(self.source)
        with self.assertRaises(BackupError):
            backup_database(link, self.root / "backup.sqlite3", BINDING)


if __name__ == "__main__":
    unittest.main()
