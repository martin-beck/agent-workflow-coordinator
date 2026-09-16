# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Negative-path tests for online SQLite backup and restore."""

from __future__ import annotations

import importlib.util
import multiprocessing
import os
import signal
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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


def _backup_then_crash_after_replace(source: str, destination: str) -> None:
    """Kill the worker after atomic replacement and before directory fsync."""

    def crash(_directory: Path) -> None:
        os.kill(os.getpid(), signal.SIGKILL)

    with patch.object(MODULE, "_fsync_directory", side_effect=crash):
        MODULE._install(Path(source), Path(destination), BINDING)


def _manifest_then_crash_before_directory_fsync(path: str, manifest: dict[str, object]) -> None:
    """Kill after manifest replacement and before its directory durability barrier."""

    def crash(_directory: Path) -> None:
        os.kill(os.getpid(), signal.SIGKILL)

    with patch.object(MODULE, "_fsync_directory", side_effect=crash):
        write_manifest(Path(path), manifest)


def _restore_then_crash_after_replace(
    backup: str, destination: str, manifest: dict[str, object]
) -> None:
    """Kill after verified restore replacement and before directory fsync."""

    def crash(_directory: Path) -> None:
        os.kill(os.getpid(), signal.SIGKILL)

    with patch.object(MODULE, "_fsync_directory", side_effect=crash):
        restore_database(Path(backup), Path(destination), manifest, BINDING, quiesced=True)


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
        with closing(sqlite3.connect(destination)) as connection:
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

    def test_failed_replace_preserves_existing_destination(self) -> None:
        backup = self.root / "backup.sqlite3"
        manifest = backup_database(self.source, backup, BINDING)
        destination = self.root / "restored.sqlite3"
        create_database(destination, body="known-good")
        with (
            patch.object(Path, "replace", side_effect=OSError("replace failed")),
            self.assertRaises(BackupError),
        ):
            restore_database(backup, destination, manifest, BINDING, quiesced=True)
        with closing(sqlite3.connect(destination)) as connection:
            self.assertEqual(
                "known-good", connection.execute("SELECT body FROM records").fetchone()[0]
            )

    def test_process_death_after_backup_replace_reopens_complete_destination(self) -> None:
        destination = self.root / "restored.sqlite3"
        create_database(destination, body="known-good")
        process = multiprocessing.get_context("fork").Process(
            target=_backup_then_crash_after_replace,
            args=(str(self.source), str(destination)),
        )
        process.start()
        process.join(timeout=10)
        self.assertEqual(-signal.SIGKILL, process.exitcode)
        self.assertFalse(process.is_alive())
        MODULE._integrity(destination, BINDING)
        with closing(sqlite3.connect(destination)) as connection:
            self.assertEqual("before", connection.execute("SELECT body FROM records").fetchone()[0])

    def test_process_death_after_manifest_replace_reopens_verified_control(self) -> None:
        backup = self.root / "backup.sqlite3"
        manifest = backup_database(self.source, backup, BINDING)
        manifest_path = self.root / "backup-manifest.json"
        process = multiprocessing.get_context("fork").Process(
            target=_manifest_then_crash_before_directory_fsync,
            args=(str(manifest_path), manifest),
        )
        process.start()
        process.join(timeout=10)
        self.assertEqual(-signal.SIGKILL, process.exitcode)
        self.assertFalse(process.is_alive())
        reopened = MODULE.json.loads(manifest_path.read_text(encoding="utf-8"))
        MODULE._validate_manifest(reopened)
        self.assertEqual(manifest["database_sha256"], reopened["database_sha256"])

    def test_ambiguous_restore_reopens_and_idempotently_retries(self) -> None:
        backup = self.root / "backup.sqlite3"
        manifest = backup_database(self.source, backup, BINDING)
        destination = self.root / "restored.sqlite3"
        create_database(destination, body="known-good")
        process = multiprocessing.get_context("fork").Process(
            target=_restore_then_crash_after_replace,
            args=(str(backup), str(destination), manifest),
        )
        process.start()
        process.join(timeout=10)
        self.assertEqual(-signal.SIGKILL, process.exitcode)
        self.assertFalse(process.is_alive())
        MODULE._integrity(destination, BINDING)
        self.assertEqual(manifest["database_sha256"], MODULE._digest(destination))
        restore_database(backup, destination, manifest, BINDING, quiesced=True)
        MODULE._integrity(destination, BINDING)
        self.assertEqual(manifest["database_sha256"], MODULE._digest(destination))

    def test_failed_directory_fsync_restores_existing_destination(self) -> None:
        backup = self.root / "backup.sqlite3"
        manifest = backup_database(self.source, backup, BINDING)
        destination = self.root / "restored.sqlite3"
        create_database(destination, body="known-good")
        with (
            patch.object(MODULE, "_fsync_directory", side_effect=[OSError("fsync failed"), None]),
            self.assertRaises(BackupError),
        ):
            restore_database(backup, destination, manifest, BINDING, quiesced=True)
        with closing(sqlite3.connect(destination)) as connection:
            self.assertEqual(
                "known-good", connection.execute("SELECT body FROM records").fetchone()[0]
            )

    def test_cleanup_failure_is_reported_fail_closed(self) -> None:
        backup = self.root / "backup.sqlite3"
        with (
            patch.object(MODULE, "_unlink", side_effect=BackupError("cleanup failed")),
            self.assertRaises(BackupError),
        ):
            backup_database(self.source, backup, BINDING)
        self.assertTrue(backup.exists())

    def test_atomic_manifest_replace_and_fsync_failures_are_reported(self) -> None:
        backup = self.root / "backup.sqlite3"
        manifest = backup_database(self.source, backup, BINDING)
        manifest_path = self.root / "manifest.json"
        with (
            patch.object(MODULE, "_fsync_directory", side_effect=OSError("fsync failed")),
            self.assertRaises(BackupError),
        ):
            write_manifest(manifest_path, manifest)

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

    def test_malformed_source_and_corrupt_backup_fail_closed(self) -> None:
        malformed = self.root / "malformed.sqlite3"
        sqlite3.connect(malformed).close()
        with self.assertRaises(BackupError):
            backup_database(malformed, self.root / "backup.sqlite3", BINDING)
        with self.assertRaises(BackupError):
            MODULE._integrity(self.root / "missing.sqlite3", BINDING)

    def test_foreign_key_integrity_failure_is_rejected(self) -> None:
        invalid = self.root / "invalid.sqlite3"
        with closing(sqlite3.connect(invalid)) as connection, connection:
            connection.executescript(
                "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "CREATE TABLE parent(id INTEGER PRIMARY KEY);"
                "CREATE TABLE child(parent_id INTEGER REFERENCES parent(id));"
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
            connection.execute("INSERT INTO child VALUES (99)")
        with self.assertRaises(BackupError):
            MODULE._integrity(invalid, BINDING)

    def test_cleanup_and_preservation_failures_are_normalized(self) -> None:
        with (
            patch.object(Path, "unlink", side_effect=OSError("unlink failed")),
            self.assertRaises(BackupError),
        ):
            MODULE._unlink(self.root / "temporary.sqlite3")
        destination = self.root / "restored.sqlite3"
        create_database(destination, body="known-good")
        with (
            patch.object(MODULE.os, "link", side_effect=OSError("link failed")),
            self.assertRaises(BackupError),
        ):
            MODULE._backup_existing(destination)

    def test_restore_failure_is_classified_as_ambiguous(self) -> None:
        destination = self.root / "restored.sqlite3"
        previous = self.root / "previous.sqlite3"
        previous.write_bytes(b"known-good")
        with (
            patch.object(Path, "replace", side_effect=OSError("restore failed")),
            self.assertRaisesRegex(BackupError, "authority restore was ambiguous"),
        ):
            MODULE._restore_existing(destination, previous)

    def test_manifest_verification_claims_are_strict(self) -> None:
        backup = self.root / "backup.sqlite3"
        manifest = backup_database(self.source, backup, BINDING)
        manifest["wal_consistent"] = False
        with self.assertRaisesRegex(BackupError, "invalid verification claims"):
            write_manifest(self.root / "manifest.json", manifest)

    def test_failed_install_without_prior_destination_removes_new_authority(self) -> None:
        backup = self.root / "backup.sqlite3"
        manifest = backup_database(self.source, backup, BINDING)
        destination = self.root / "restored.sqlite3"
        with (
            patch.object(MODULE, "_fsync_directory", side_effect=OSError("fsync failed")),
            self.assertRaises(BackupError),
        ):
            restore_database(backup, destination, manifest, BINDING, quiesced=True)
        self.assertFalse(destination.exists())

    def test_source_symlink_is_rejected(self) -> None:
        link = self.root / "source-link.sqlite3"
        link.symlink_to(self.source)
        with self.assertRaises(BackupError):
            backup_database(link, self.root / "backup.sqlite3", BINDING)

    def test_symlinked_destination_parent_is_rejected_before_install(self) -> None:
        real_parent = self.root / "real-parent"
        real_parent.mkdir()
        linked_parent = self.root / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaisesRegex(BackupError, "parent must not contain symlinks"):
            backup_database(self.source, linked_parent / "backup.sqlite3", BINDING)

        backup = self.root / "backup.sqlite3"
        manifest = backup_database(self.source, backup, BINDING)
        destination = linked_parent / "restored.sqlite3"
        with self.assertRaisesRegex(BackupError, "parent must not contain symlinks"):
            restore_database(backup, destination, manifest, BINDING, quiesced=True)
        self.assertFalse((real_parent / "restored.sqlite3").exists())

    def test_symlinked_manifest_parent_is_rejected_before_publication(self) -> None:
        backup = self.root / "backup.sqlite3"
        manifest = backup_database(self.source, backup, BINDING)
        real_parent = self.root / "manifest-parent"
        real_parent.mkdir()
        linked_parent = self.root / "manifest-link"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaisesRegex(BackupError, "parent must not contain symlinks"):
            write_manifest(linked_parent / "manifest.json", manifest)
        self.assertFalse((real_parent / "manifest.json").exists())

    def test_parent_identity_drift_fails_before_sqlite_publication(self) -> None:
        backup = self.root / "backup.sqlite3"
        manifest = backup_database(self.source, backup, BINDING)
        destination = self.root / "restored.sqlite3"
        with (
            patch.object(MODULE, "_parent_identity", side_effect=[(1, 1), (1, 2)]),
            self.assertRaisesRegex(BackupError, "parent identity changed"),
        ):
            restore_database(backup, destination, manifest, BINDING, quiesced=True)
        self.assertFalse(destination.exists())
        manifest_path = self.root / "manifest.json"
        with (
            patch.object(MODULE, "_parent_identity", side_effect=[(1, 1), (1, 2)]),
            self.assertRaisesRegex(BackupError, "parent identity changed"),
        ):
            write_manifest(manifest_path, manifest)
        self.assertFalse(manifest_path.exists())


if __name__ == "__main__":
    unittest.main()
