# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Durable opt-in receipt intent tests; queued is never completed."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tools.fast_receipts import ReceiptConflictError, ReceiptStore


def _independent_enqueue(arguments: tuple[str, str, int]) -> tuple[str, str]:
    path, project_id, worker = arguments
    store = ReceiptStore(Path(path), project_id)
    try:
        receipt = store.enqueue_heartbeat(
            key=f"worker-{worker}:heartbeat",
            task=f"AR-{worker:04d}",
            owner=f"worker-{worker}",
            expected_revision=1,
            lease_minutes=20,
        )
        return str(receipt["receipt_id"]), str(receipt["phase"])
    finally:
        store.close()


def _same_key_enqueue(arguments: tuple[str, str]) -> str:
    path, project_id = arguments
    store = ReceiptStore(Path(path), project_id)
    try:
        receipt = store.enqueue_heartbeat(
            key="shared:heartbeat:1",
            task="AR-0120",
            owner="worker-a",
            expected_revision=1,
            lease_minutes=20,
        )
        return str(receipt["receipt_id"])
    finally:
        store.close()


class FastReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "private" / "receipts.sqlite3"
        self.project_id = str(uuid.uuid4())

    def store(self) -> ReceiptStore:
        result = ReceiptStore(self.path, self.project_id)
        self.addCleanup(result.close)
        return result

    def submit(self, store: ReceiptStore, **changes: object) -> dict[str, object]:
        fields: dict[str, object] = {
            "key": "worker-a:heartbeat:1",
            "task": "AR-0120",
            "owner": "worker-a",
            "expected_revision": 1,
            "lease_minutes": 20,
        }
        fields.update(changes)
        return store.enqueue_heartbeat(**fields)  # type: ignore[arg-type]

    def test_retry_returns_same_durable_queued_receipt_after_reopen(self) -> None:
        first_store = self.store()
        first = self.submit(first_store)
        self.assertEqual("queued-local", first["phase"])
        self.assertEqual("heartbeat", first["operation"])
        first_store.close()
        reopened = self.store()
        second = self.submit(reopened)
        self.assertEqual(first["receipt_id"], second["receipt_id"])
        self.assertEqual(first, reopened.read(str(first["receipt_id"])))

    def test_same_key_different_input_rejects_without_new_intent(self) -> None:
        store = self.store()
        first = self.submit(store)
        with self.assertRaises(ReceiptConflictError):
            self.submit(store, lease_minutes=21)
        with self.assertRaises(ReceiptConflictError):
            self.submit(store, expected_revision=2)
        self.assertEqual(first, store.read(str(first["receipt_id"])))
        count = store.connection.execute("SELECT count(*) FROM intents").fetchone()[0]
        self.assertEqual(1, count)

    def test_invalid_intent_never_creates_row(self) -> None:
        store = self.store()
        for change in (
            {"key": "../escape"},
            {"task": "../foreign"},
            {"owner": "worker with spaces"},
            {"expected_revision": 0},
            {"lease_minutes": 0},
            {"lease_minutes": 1441},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.submit(store, **change)
        self.assertEqual(0, store.connection.execute("SELECT count(*) FROM intents").fetchone()[0])

    def test_foreign_project_cannot_open_bound_database(self) -> None:
        store = self.store()
        self.submit(store)
        with self.assertRaisesRegex(RuntimeError, "different project"):
            ReceiptStore(self.path, str(uuid.uuid4()))

    def test_symlinked_database_is_rejected(self) -> None:
        self.path.parent.mkdir(mode=0o700)
        target = self.path.parent / "target"
        target.write_text("not a database")
        self.path.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            ReceiptStore(self.path, self.project_id)

    def test_nonprivate_parent_is_rejected(self) -> None:
        self.path.parent.mkdir(mode=0o777)
        self.path.parent.chmod(0o755)
        with self.assertRaisesRegex(RuntimeError, "not private"):
            ReceiptStore(self.path, self.project_id)

    def test_existing_hardlink_is_rejected(self) -> None:
        self.path.parent.mkdir(mode=0o700)
        original = self.path.parent / "other"
        original.write_bytes(b"")
        original.chmod(0o600)
        os.link(original, self.path)
        with self.assertRaisesRegex(RuntimeError, "private regular owned"):
            ReceiptStore(self.path, self.project_id)

    def test_database_replacement_is_rejected_before_enqueue(self) -> None:
        store = self.store()
        original = self.path.parent / "original"
        self.path.rename(original)
        self.path.write_bytes(b"replacement")
        self.path.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            self.submit(store)

    def test_unsafe_wal_sidecar_is_rejected_before_enqueue(self) -> None:
        store = self.store()
        target = self.path.parent / "target"
        target.write_bytes(b"not a wal")
        wal = Path(str(self.path) + "-wal")
        wal.unlink(missing_ok=True)
        wal.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "sidecar is unsafe"):
            self.submit(store)

    def test_rejects_nonlocal_filesystem_before_database_open(self) -> None:
        with (
            patch(
                "tools.fast_receipts.require_local_filesystem",
                side_effect=RuntimeError("SQLITE_UNSUPPORTED_FILESYSTEM"),
            ),
            self.assertRaisesRegex(RuntimeError, "SQLITE_UNSUPPORTED_FILESYSTEM"),
        ):
            ReceiptStore(self.path, self.project_id)
        self.assertFalse(self.path.exists())

    def test_rejects_non_wal_connection(self) -> None:
        memory = sqlite3.connect(":memory:", isolation_level=None)
        with (
            patch("tools.fast_receipts.sqlite3.connect", return_value=memory),
            self.assertRaisesRegex(RuntimeError, "SQLITE_WAL_UNAVAILABLE"),
        ):
            ReceiptStore(self.path, self.project_id)

    def test_sixteen_independent_submitters_preserve_all_intents(self) -> None:
        store = self.store()
        arguments = [(str(self.path), self.project_id, worker) for worker in range(16)]
        with ProcessPoolExecutor(max_workers=16) as executor:
            results = list(executor.map(_independent_enqueue, arguments))
        self.assertEqual(16, len({receipt_id for receipt_id, _ in results}))
        self.assertTrue(all(phase == "queued-local" for _, phase in results))
        count = store.connection.execute("SELECT count(*) FROM intents").fetchone()[0]
        self.assertEqual(16, count)

    def test_sixteen_independent_retries_share_one_receipt(self) -> None:
        store = self.store()
        arguments = [(str(self.path), self.project_id)] * 16
        with ProcessPoolExecutor(max_workers=16) as executor:
            receipt_ids = list(executor.map(_same_key_enqueue, arguments))
        self.assertEqual(1, len(set(receipt_ids)))
        count = store.connection.execute("SELECT count(*) FROM intents").fetchone()[0]
        self.assertEqual(1, count)


if __name__ == "__main__":
    unittest.main()
