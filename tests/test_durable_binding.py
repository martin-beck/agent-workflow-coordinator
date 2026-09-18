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

                def operation_lock(self) -> _Lock:
                    return _Lock()

            session = SQLiteCompatibilitySession(binding, Store())
            session.assert_current()
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

                def operation_lock(self) -> _Lock:
                    return _Lock()

            with self.assertRaisesRegex(DurableBindingError, "foreign authority"):
                SQLiteCompatibilitySession(binding, Store())


class _Lock(AbstractContextManager[None]):
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: object) -> bool | None:
        return None


if __name__ == "__main__":
    unittest.main()
