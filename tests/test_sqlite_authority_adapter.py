# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile tests for the read-only SQLite authority adapter slice."""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

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


def _snapshot_process(path_text: str, crash: bool) -> None:
    adapter = SQLiteAuthorityAdapter(Path(path_text))
    adapter.snapshot("discover", CONTEXT)
    if crash:
        os._exit(17)


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
        self.assertFalse(self.adapter.verify_rollback_context(CONTEXT)["rollback_context_verified"])

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

    def test_descriptor_replacement_fails_old_reader_and_fresh_reader_recovers(self) -> None:
        replacement = self.root / "replacement.sqlite"
        replacement.write_bytes(self.authority.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(self.authority)
        with self.assertRaisesRegex(SQLiteAuthorityError, "identity changed"):
            self.adapter.snapshot("discover", CONTEXT)
        self.assertTrue(
            SQLiteAuthorityAdapter(self.authority).snapshot("discover", CONTEXT)[
                "sqlite_integrity_verified"
            ]
        )

    def test_process_death_after_read_allows_fresh_reader(self) -> None:
        context = multiprocessing.get_context("fork")
        worker = context.Process(target=_snapshot_process, args=(str(self.authority), True))
        worker.start()
        worker.join(5)
        self.assertEqual(17, worker.exitcode)
        self.assertTrue(
            SQLiteAuthorityAdapter(self.authority).snapshot("discover", CONTEXT)[
                "sqlite_integrity_verified"
            ]
        )

    def test_new_or_replaced_wal_sidecar_fails_old_reader_closed(self) -> None:
        sidecar = self.authority.with_name(self.authority.name + "-wal")
        sidecar.write_bytes(b"wal")
        sidecar.chmod(0o600)
        with self.assertRaisesRegex(SQLiteAuthorityError, "identity changed"):
            self.adapter.snapshot("discover", CONTEXT)
        sidecar.unlink()

    def test_unsafe_sidecar_is_rejected_at_construction(self) -> None:
        sidecar = self.authority.with_name(self.authority.name + "-shm")
        sidecar.write_bytes(b"shm")
        sidecar.chmod(0o644)
        with self.assertRaisesRegex(SQLiteAuthorityError, "sidecar is unsafe"):
            SQLiteAuthorityAdapter(self.authority)

    def test_unavailable_and_unsafe_descriptors_fail_closed(self) -> None:
        with self.assertRaisesRegex(SQLiteAuthorityError, "unavailable"):
            SQLiteAuthorityAdapter(self.root / "missing.sqlite")
        self.authority.chmod(0o644)
        with self.assertRaisesRegex(SQLiteAuthorityError, "unsafe"):
            SQLiteAuthorityAdapter(self.authority)

    def test_non_ok_integrity_result_fails_closed(self) -> None:
        class Cursor:
            def __init__(self, value: object) -> None:
                self.value = value

            def fetchone(self) -> tuple[object]:
                return (self.value,)

            def __iter__(self) -> Iterator[object]:
                return iter(())

        class Connection:
            def execute(self, statement: str) -> Cursor:
                return Cursor("not-ok" if "integrity" in statement else None)

            def close(self) -> None:
                pass

        with (
            patch("tools.sqlite_authority_adapter.sqlite3.connect", return_value=Connection()),
            self.assertRaisesRegex(SQLiteAuthorityError, "not clean"),
        ):
            self.adapter.snapshot("discover", CONTEXT)


if __name__ == "__main__":
    unittest.main()
