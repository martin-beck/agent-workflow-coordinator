# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile and durability tests for the SQLite rollback control store."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

from tools.rollback_control_store import (
    ControlStoreError,
    SQLiteControlStoreAdapter,
    SQLiteRollbackControlStore,
    bind_control_store,
    canonical_barrier_digest,
    canonical_envelope_digest,
)

PROJECT = "11111111-1111-4111-8111-111111111111"
RECORD = {
    "operation_id": "op-1",
    "project_id": PROJECT,
    "state_revision": 1,
    "fencing_token": "fence-1",
    "fencing_owner": "owner-1",
    "backend": "sqlite",
    "authority_revision": "authority-1",
    "durable_barrier_id": "barrier-1",
    "barrier_identity_digest": "a" * 64,
    "envelope_digest": "b" * 64,
    "target": "rollback",
    "status": "held",
    "revision": 1,
}
RECORD["barrier_identity_digest"] = canonical_barrier_digest(RECORD)
RECORD["envelope_digest"] = canonical_envelope_digest(RECORD)


class RollbackControlStoreTests(unittest.TestCase):
    def test_canonical_barrier_digest_is_stable_and_excludes_mutable_fields(self) -> None:
        first = canonical_barrier_digest(RECORD)
        second = canonical_barrier_digest({**RECORD, "status": "ambiguous", "revision": 99})
        self.assertEqual(first, second)
        self.assertEqual(64, len(first))

    def test_binding_is_sqlite_only_and_store_owned(self) -> None:
        class Delegate:
            def snapshot(self, _phase: str, _context: Mapping[str, object]) -> Mapping[str, object]:
                return {}

            def execute(self, _phase: str, _context: Mapping[str, object]) -> Mapping[str, object]:
                return {}

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRollbackControlStore(Path(directory) / "control.sqlite", PROJECT)
            with self.assertRaises(ControlStoreError):
                bind_control_store("sqlite", Delegate(), store)
            authority = Path(directory) / "authority.sqlite"
            authority.touch()
            bound = SQLiteRollbackControlStore(Path(directory) / "bound.sqlite", PROJECT, authority)
            self.assertIsInstance(
                bind_control_store("sqlite", Delegate(), bound), SQLiteControlStoreAdapter
            )
            with self.assertRaises(ControlStoreError):
                bind_control_store("git", Delegate(), None)

    def test_wal_cas_and_reload_are_durable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRollbackControlStore(Path(directory) / "control.sqlite", PROJECT)
            created = store.cas(0, {**RECORD, "revision": 1})
            self.assertEqual(1, created["revision"])
            self.assertEqual(created, store.snapshot("op-1"))
            self.assertTrue(store.verify_rollback_context(created))
            self.assertFalse(
                store.verify_rollback_context({**created, "envelope_digest": "e" * 64})
            )
            releasing = store.cas(1, {**created, "status": "releasing", "revision": 2})
            released = store.cas(2, {**releasing, "status": "released", "revision": 3})
            self.assertEqual("released", released["status"])
            self.assertEqual(3, store.snapshot("op-1")["revision"])

    def test_status_transition_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRollbackControlStore(Path(directory) / "control.sqlite", PROJECT)
            store.cas(0, RECORD)
            releasing = store.cas(1, {**RECORD, "status": "releasing", "revision": 2})
            with self.assertRaises(ControlStoreError):
                store.cas(2, {**releasing, "status": "held", "revision": 3})

    def test_ambiguous_requires_explicit_newer_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRollbackControlStore(Path(directory) / "control.sqlite", PROJECT)
            ambiguous = store.cas(0, RECORD)
            ambiguous = store.cas(1, {**ambiguous, "status": "ambiguous", "revision": 2})
            with self.assertRaises(ControlStoreError):
                store.reconcile_ambiguous("op-1", {**RECORD, "operation_id": "op-2"})
            replacement = {
                **RECORD,
                "operation_id": "op-2",
                "state_revision": 2,
                "fencing_token": "fence-2",
            }
            replacement["barrier_identity_digest"] = canonical_barrier_digest(replacement)
            replacement["envelope_digest"] = canonical_envelope_digest(replacement)
            recovered = store.reconcile_ambiguous("op-1", replacement)
            self.assertEqual("held", recovered["status"])

    def test_schema_corruption_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.sqlite"
            store = SQLiteRollbackControlStore(path, PROJECT)
            store.cas(0, RECORD)
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE control_meta SET value='99' WHERE key='schema_version'")
            with self.assertRaises(ControlStoreError):
                store.snapshot("op-1")

    def test_symlink_and_authority_alias_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = root / "authority.sqlite"
            authority.touch()
            link = root / "control-link.sqlite"
            link.symlink_to(authority)
            with self.assertRaises(ControlStoreError):
                SQLiteRollbackControlStore(link, PROJECT)
            with self.assertRaises(ControlStoreError):
                SQLiteRollbackControlStore(authority, PROJECT, authority)

    def test_with_barrier_holds_coordinator_lock_through_authority_callback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRollbackControlStore(Path(directory) / "control.sqlite", PROJECT)
            seen: list[str] = []

            def authority(record: Mapping[str, object]) -> Mapping[str, object]:
                seen.append(str(record["status"]))
                return {**record, "status": "releasing"}

            result = store.with_barrier(0, RECORD, authority)
            self.assertEqual(["held"], seen)
            self.assertEqual("releasing", result["status"])

    def test_with_barrier_failure_leaves_durable_held_barrier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRollbackControlStore(Path(directory) / "control.sqlite", PROJECT)

            def fail(_record: Mapping[str, object]) -> Mapping[str, object]:
                raise RuntimeError("authority failed")

            with self.assertRaises(RuntimeError):
                store.with_barrier(0, RECORD, fail)
            self.assertEqual("held", store.snapshot("op-1")["status"])

    def test_cas_conflict_and_binding_mismatch_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRollbackControlStore(Path(directory) / "control.sqlite", PROJECT)
            store.cas(0, RECORD)
            with self.assertRaises(ControlStoreError):
                store.cas(0, {**RECORD, "revision": 1})
            with self.assertRaises(ControlStoreError):
                store.cas(1, {**RECORD, "project_id": "22222222-2222-4222-8222-222222222222"})
            with self.assertRaises(ControlStoreError):
                store.cas(0, {**RECORD, "operation_id": "op-2"})

    def test_invalid_identity_and_status_are_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRollbackControlStore(Path(directory) / "control.sqlite", PROJECT)
            for mutation in (
                {"project_id": "project-1"},
                {"backend": "git"},
                {"envelope_digest": "f" * 63},
                {"operation_id": "../escape"},
            ):
                with self.subTest(mutation=mutation), self.assertRaises(ControlStoreError):
                    store.cas(0, {**RECORD, **mutation})
            with self.assertRaises(ControlStoreError):
                store.snapshot("op-1")


if __name__ == "__main__":
    unittest.main()
