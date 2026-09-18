# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Contract and hostile tests for the authority-neutral durable binding seam."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import AbstractContextManager
from pathlib import Path

from tools.durable_binding import (
    DurableBindingError,
    FilesystemAuthorityBinding,
    SQLiteCompatibilitySession,
)


class DurableBindingTests(unittest.TestCase):
    def test_git_and_sqlite_bindings_capture_distinct_authority_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git_root = root / "git"
            git_root.mkdir(mode=0o700)
            sqlite_path = root / "authority.sqlite"
            with sqlite3.connect(sqlite_path) as connection:
                connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY)")
            sqlite_path.chmod(0o600)
            git_binding = FilesystemAuthorityBinding.bind(git_root, "git")
            sqlite_binding = FilesystemAuthorityBinding.bind(sqlite_path, "sqlite")
            self.assertEqual("git", git_binding.identity.kind)
            self.assertEqual("sqlite", sqlite_binding.identity.kind)
            self.assertNotEqual(git_binding.identity, sqlite_binding.identity)

    def test_replacement_and_symlink_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = root / "authority.sqlite"
            authority.touch(mode=0o600)
            binding = FilesystemAuthorityBinding.bind(authority, "sqlite")
            replacement = root / "replacement.sqlite"
            replacement.touch(mode=0o600)
            authority.unlink()
            replacement.rename(authority)
            with self.assertRaisesRegex(DurableBindingError, "identity changed"):
                binding.assert_current()
            authority.unlink()
            authority.symlink_to(replacement)
            with self.assertRaisesRegex(DurableBindingError, "symlinks"):
                binding.assert_current()

    def test_sqlite_compatibility_session_requires_matching_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = root / "authority.sqlite"
            with sqlite3.connect(authority) as connection:
                connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY)")
            authority.chmod(0o600)
            binding = FilesystemAuthorityBinding.bind(authority, "sqlite")

            class Store:
                authority_path = authority
                control_store_path = root / "control.sqlite"
                control_lock_path = root / "control.lock"

                def operation_lock(self) -> _Lock:
                    return _Lock()

            Store.control_store_path.touch(mode=0o600)
            Store.control_lock_path.touch(mode=0o600)
            journal = root / "journal.json"
            journal.write_bytes(b'{"status":"running"}\n')
            session = SQLiteCompatibilitySession(binding, Store(), journal)
            session.assert_current()
            snapshot = session.snapshot()
            self.assertEqual(snapshot, session.assert_snapshot_current(snapshot))
            with session.operation_lock():
                pass
            with self.assertRaisesRegex(DurableBindingError, "outcome publication"):
                session.record_outcome("operation", {})

    def test_sqlite_compatibility_rejects_foreign_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = root / "authority.sqlite"
            foreign = root / "foreign.sqlite"
            authority.touch(mode=0o600)
            foreign.touch(mode=0o600)
            binding = FilesystemAuthorityBinding.bind(authority, "sqlite")

            class Store:
                authority_path = foreign
                control_store_path = root / "control.sqlite"
                control_lock_path = root / "control.lock"

                def operation_lock(self) -> _Lock:
                    return _Lock()

            with self.assertRaisesRegex(DurableBindingError, "foreign authority"):
                SQLiteCompatibilitySession(binding, Store(), root / "journal.json")

    def test_sqlite_snapshot_rejects_journal_replacement_as_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = root / "authority.sqlite"
            with sqlite3.connect(authority) as connection:
                connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY)")
            authority.chmod(0o600)
            control = root / "control.sqlite"
            lock = root / "control.lock"
            control.touch(mode=0o600)
            lock.touch(mode=0o600)
            journal = root / "journal.json"
            journal.write_bytes(b'{"status":"running"}\n')
            binding = FilesystemAuthorityBinding.bind(authority, "sqlite")

            class Store:
                authority_path = authority
                control_store_path = control
                control_lock_path = lock

                @staticmethod
                def operation_lock() -> _Lock:
                    return _Lock()

            session = SQLiteCompatibilitySession(binding, Store(), journal)
            expected = session.snapshot()
            replacement = root / "replacement-journal.json"
            replacement.write_bytes(b'{"status":"replaced"}\n')
            journal.unlink()
            replacement.rename(journal)
            with self.assertRaisesRegex(DurableBindingError, "ambiguous"):
                session.assert_snapshot_current(expected)

    def test_sqlite_snapshot_lock_close_failure_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = root / "authority.sqlite"
            authority.touch(mode=0o600)
            control = root / "control.sqlite"
            lock = root / "control.lock"
            journal = root / "journal.json"
            control.touch(mode=0o600)
            lock.touch(mode=0o600)
            journal.write_bytes(b'{"status":"running"}\n')
            binding = FilesystemAuthorityBinding.bind(authority, "sqlite")

            class BrokenStore:
                authority_path = authority
                control_store_path = control
                control_lock_path = lock

                @staticmethod
                def operation_lock() -> _Lock:
                    raise OSError("simulated close uncertainty")

            session = SQLiteCompatibilitySession(binding, BrokenStore(), journal)
            with self.assertRaisesRegex(DurableBindingError, "ambiguous"):
                session.snapshot()

    def test_sqlite_snapshot_rejects_journal_content_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = root / "authority.sqlite"
            authority.touch(mode=0o600)
            control = root / "control.sqlite"
            lock = root / "control.lock"
            journal = root / "journal.json"
            control.touch(mode=0o600)
            lock.touch(mode=0o600)
            journal.write_bytes(b'{"status":"running"}\n')
            binding = FilesystemAuthorityBinding.bind(authority, "sqlite")

            class Store:
                authority_path = authority
                control_store_path = control
                control_lock_path = lock

                @staticmethod
                def operation_lock() -> _Lock:
                    return _Lock()

            session = SQLiteCompatibilitySession(binding, Store(), journal)
            expected = session.snapshot()
            journal.write_bytes(b'{"status":"ambiguous"}\n')
            with self.assertRaisesRegex(DurableBindingError, "ambiguous"):
                session.assert_snapshot_current(expected)


class _Lock(AbstractContextManager[None]):
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: object) -> bool | None:
        return None


if __name__ == "__main__":
    unittest.main()
