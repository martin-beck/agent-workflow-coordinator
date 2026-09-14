# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile tests for the read-only SQLite authority adapter slice."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from tools.scoped_backend_adapter import ScopedBackendAdapter
from tools.sqlite_authority_adapter import SQLiteAuthorityAdapter, SQLiteAuthorityError

CONTEXT = {
    "schema_version": 2,
    "backend": "sqlite",
    "project_id": "project",
    "operation_id": "op-1",
    "state_revision": 1,
    "authority_revision": "authority",
    "fencing_token": "fence",
    "fencing_owner": "owner",
    "durable_barrier_id": "barrier",
    "artifact_root": "/artifacts",
    "source": "/source",
    "destination": "/destination",
    "manifest": "/manifest",
    "barrier_identity_digest": "0" * 64,
    "target": "new",
    "envelope_digest": "0" * 64,
}


class Scope:
    def assert_ordered(self) -> None:
        pass

    def assert_context(self, _context: Mapping[str, object]) -> None:
        pass

    @contextmanager
    def hold(self) -> Iterator[object]:
        yield object()


class SQLiteAuthorityAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.authority = self.root / "authority.sqlite"
        with sqlite3.connect(self.authority) as connection:
            connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, body TEXT)")
            connection.execute("INSERT INTO records(body) VALUES ('clean')")
        self.authority.chmod(0o600)
        self.adapter = SQLiteAuthorityAdapter(self.authority)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_clean_snapshot_is_integrity_bound_and_nonmutating(self) -> None:
        result = self.adapter.snapshot("discover", CONTEXT)
        self.assertTrue(result["sqlite_integrity_verified"])
        self.assertTrue(result["sqlite_foreign_keys_verified"])
        self.assertFalse(result["mutates_authority"])
        with self.assertRaisesRegex(SQLiteAuthorityError, "not implemented"):
            self.adapter.execute("commit", CONTEXT)

    def test_scoped_wrapper_guards_disabled_execute(self) -> None:
        adapter = ScopedBackendAdapter(self.adapter, Scope())
        self.assertTrue(adapter.snapshot("discover", CONTEXT)["sqlite_integrity_verified"])
        with self.assertRaisesRegex(SQLiteAuthorityError, "not implemented"):
            adapter.execute("commit", CONTEXT)

    def test_corrupt_or_mismatched_authority_fails_closed(self) -> None:
        self.authority.write_bytes(b"not sqlite")
        with self.assertRaisesRegex(SQLiteAuthorityError, "observation failed"):
            self.adapter.snapshot("discover", CONTEXT)
        with self.assertRaisesRegex(SQLiteAuthorityError, "mismatched"):
            self.adapter.snapshot("discover", {**CONTEXT, "backend": "git"})


if __name__ == "__main__":
    unittest.main()
