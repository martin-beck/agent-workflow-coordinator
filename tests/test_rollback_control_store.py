# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile and durability tests for the SQLite rollback control store."""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

from tools.rollback_control_store import ControlStoreError, SQLiteRollbackControlStore

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


class RollbackControlStoreTests(unittest.TestCase):
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
