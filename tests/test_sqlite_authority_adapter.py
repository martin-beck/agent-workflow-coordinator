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
from typing import Any, cast
from unittest.mock import patch

from tools.admission_lease import AdmissionLease, validate_recheck
from tools.handoffctl import locked
from tools.lock_domain_scope import LockDomainScope
from tools.mutation_fence import MutationFence, provision, provision_control_binding
from tools.rollback_control_store import (
    BarrierSessionState,
    SQLiteBarrierSessionStore,
    SQLiteRollbackControlStore,
)
from tools.scoped_backend_adapter import ScopedBackendAdapter
from tools.sqlite_authority_adapter import (
    SQLiteAuthorityAdapter,
    SQLiteAuthorityError,
    SQLiteLifecycleExecutor,
)
from tools.upgrade_engine import BoundRollbackCapability, PhaseContext, UpgradeEngine
from tools.upgrade_identity import (
    BarrierSessionIdentity,
    canonical_barrier_digest,
    canonical_barrier_session_digest,
    canonical_envelope_digest,
)

PROJECT = "11111111-1111-4111-8111-111111111111"

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


def _bound_snapshot_worker(root_text: str, mode: str, result: Any, rollback: bool = False) -> None:
    root = Path(root_text)
    authority = root / "authority.sqlite"
    control = root / "control.sqlite"
    store = SQLiteRollbackControlStore(control, PROJECT, authority)
    session = SQLiteBarrierSessionStore(store, lambda: "authority")
    fence = MutationFence(
        authority,
        root / "authority-marker.json",
        root / "authority-lifecycle.json",
        root / "authority.lock",
        control,
        root / "control-binding.json",
        store.control_lock_path,
    )
    old = AdmissionLease(PROJECT, "authority", "fence", "owner", "barrier", 1)
    replacement = AdmissionLease(PROJECT, "authority", "fence-new", "owner-new", "barrier-new", 2)
    lease = old if mode == "stale" else replacement
    recheck = validate_recheck(
        lease,
        project_id=PROJECT,
        authority_revision=lease.authority_revision,
        fencing_token=lease.fencing_token,
        fencing_owner=lease.fencing_owner,
        durable_barrier_id=lease.durable_barrier_id,
        revision=lease.revision,
    )
    scope = LockDomainScope.bind(session, fence, lease, recheck, locked)

    class CountingAdapter(SQLiteAuthorityAdapter):
        calls = 0

        def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, object]:
            self.calls += 1
            return super().snapshot(phase, context)

    adapter = CountingAdapter(authority)
    if mode == "crash":

        def aborting_snapshot(_phase: str, _context: Mapping[str, object]) -> dict[str, object]:
            os._exit(17)

        adapter.snapshot = aborting_snapshot  # type: ignore[assignment]
    context = {
        **CONTEXT,
        "target": "rollback" if rollback else "new",
        "project_id": PROJECT,
        "authority_revision": lease.authority_revision,
        "fencing_token": lease.fencing_token,
        "fencing_owner": lease.fencing_owner,
        "durable_barrier_id": lease.durable_barrier_id,
        "state_revision": lease.revision,
    }
    try:
        if rollback:
            value = adapter.verify_rollback_context_bound(
                context, scope, lease=lease, admission_recheck=recheck
            )
        else:
            value = adapter.snapshot_bound(
                "discover", context, scope, lease=lease, admission_recheck=recheck
            )
    except Exception as error:
        result.put(
            (
                "rejected",
                type(error).__name__,
                adapter.calls,
                session.operation_owned_by_current_thread,
            )
        )
    else:
        result.put(
            (
                "success",
                value["sqlite_integrity_verified"],
                adapter.calls,
                session.operation_owned_by_current_thread,
            )
        )


def _hold_exclusive_transaction(path_text: str, ready: object) -> None:
    connection = sqlite3.connect(path_text, isolation_level=None)
    try:
        connection.execute("BEGIN EXCLUSIVE")
        update = connection.execute(
            "UPDATE records SET body = 'uncommitted' WHERE id = 1 AND body = 'clean'"
        )
        if update.rowcount != 1:
            raise RuntimeError("CAS did not match the expected authority row")
        ready.send(True)  # type: ignore[attr-defined]
        ready.recv()  # type: ignore[attr-defined]
    finally:
        connection.close()


class Scope:
    def assert_ordered(self) -> None:
        pass

    def assert_context(self, _context: Mapping[str, object]) -> None:
        pass

    @contextmanager
    def hold(self) -> Iterator[object]:
        yield object()


class SQLiteAuthorityAdapterTests(unittest.TestCase):
    def _durable_state(self) -> tuple[bytes, object]:
        return self.authority.read_bytes(), self.session.snapshot()

    @staticmethod
    def _session_identity() -> BarrierSessionIdentity:
        record: dict[str, object] = {
            "schema_version": 1,
            "project_id": PROJECT,
            "attempt_id": "attempt-1",
            "state_revision": 1,
            "authority_revision_at_acquire": "authority",
            "durable_barrier_id": "barrier",
            "fencing_token": "fence",
            "fencing_owner": "owner",
            "identity_digest": "0" * 64,
        }
        record["identity_digest"] = canonical_barrier_session_digest(record)
        return BarrierSessionIdentity.from_record(record)

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.authority = self.root / "authority.sqlite"
        with sqlite3.connect(self.authority) as connection:
            connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, body TEXT)")
            connection.execute("INSERT INTO records(body) VALUES ('clean')")
        self.authority.chmod(0o600)
        self.adapter = SQLiteAuthorityAdapter(self.authority)
        control = self.root / "control.sqlite"
        store = SQLiteRollbackControlStore(control, PROJECT, self.authority)
        marker = self.root / "authority-marker.json"
        lifecycle = self.root / "authority-lifecycle.json"
        authority_lock = self.root / "authority.lock"
        binding = self.root / "control-binding.json"
        provision(self.authority, marker, lifecycle, authority_lock, PROJECT)
        provision_control_binding(control, binding, store.control_lock_path, PROJECT)
        fence = MutationFence(
            self.authority,
            marker,
            lifecycle,
            authority_lock,
            control,
            binding,
            store.control_lock_path,
        )
        self.session = SQLiteBarrierSessionStore(store, lambda: "authority")
        self.session.create(self._session_identity())
        self.lease = AdmissionLease(PROJECT, "authority", "fence", "owner", "barrier", 1)
        self.recheck = validate_recheck(
            self.lease,
            project_id=PROJECT,
            authority_revision="authority",
            fencing_token="fence",  # noqa: S106
            fencing_owner="owner",
            durable_barrier_id="barrier",
            revision=1,
        )
        self.scope = LockDomainScope.bind(self.session, fence, self.lease, self.recheck, locked)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_bound_lifecycle_executor_snapshots_real_control_and_journal(self) -> None:
        journal = self.root / "engine-journal.json"
        journal.write_text(
            '{"status":"running","phase":"backup","records":[{"outcome":"started"}]}\n',
            encoding="utf-8",
        )
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        snapshot = executor.snapshot()
        self.assertEqual("held", snapshot.control.status)
        self.assertEqual(1, snapshot.control.revision)
        self.assertEqual("backup", snapshot.journal.phase)
        self.assertEqual(4, len(snapshot.journal_identity))
        self.assertEqual([{"outcome": "started"}], snapshot.journal.as_mapping()["records"])
        self.assertFalse(self.session.operation_owned_by_current_thread)
        self.assertEqual(b"clean", self.authority.read_bytes()[-5:])

    def test_bound_lifecycle_executor_backup_failure_rereads_and_preserves_state(self) -> None:
        journal = self.root / "engine-journal.json"
        journal.write_text('{"status":"running","phase":"backup","records":[]}\n', encoding="utf-8")
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        before = executor.snapshot()
        with (
            patch.object(self.adapter, "backup_bound", side_effect=RuntimeError("injected backup")),
            self.assertRaisesRegex(SQLiteAuthorityError, "lifecycle effect failed"),
        ):
            executor.backup(self.root / "backup.sqlite", {})
        self.assertEqual(before, executor.snapshot())
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bound_lifecycle_executor_restore_failure_rereads_and_preserves_state(self) -> None:
        journal = self.root / "engine-journal.json"
        journal.write_text(
            '{"status":"running","phase":"rollback","records":[]}\n', encoding="utf-8"
        )
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        before = executor.snapshot()
        with (
            patch.object(
                self.adapter, "restore_bound", side_effect=RuntimeError("injected restore")
            ),
            self.assertRaisesRegex(SQLiteAuthorityError, "lifecycle effect failed"),
        ):
            executor.restore(self.root / "backup.sqlite", self.root / "destination.sqlite", {}, {})
        self.assertEqual(before, executor.snapshot())
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bound_lifecycle_executor_rejects_failed_effect_journal_mutation(self) -> None:
        journal = self.root / "engine-journal.json"
        journal.write_text('{"status":"running","phase":"backup","records":[]}\n', encoding="utf-8")
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        before = executor.snapshot()

        def mutate_then_fail(_destination: Path, _binding: dict[str, Any]) -> dict[str, Any]:
            journal.write_text(
                '{"status":"failed","phase":"backup","records":[]}\n', encoding="utf-8"
            )
            raise RuntimeError("injected journal mutation")

        with (
            patch.object(self.adapter, "backup_bound", side_effect=mutate_then_fail),
            self.assertRaisesRegex(SQLiteAuthorityError, "durable lifecycle state changed"),
        ):
            executor.backup(self.root / "backup.sqlite", {})
        self.assertNotEqual(before.journal, executor.snapshot().journal)
        self.assertFalse((self.root / "backup.sqlite").exists())
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bound_lifecycle_executor_rejects_failed_effect_control_mutation(self) -> None:
        journal = self.root / "engine-journal.json"
        journal.write_text('{"status":"running","phase":"backup","records":[]}\n', encoding="utf-8")
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        before = executor.snapshot()

        def mutate_then_fail(_destination: Path, _binding: dict[str, Any]) -> dict[str, Any]:
            # Simulate a hostile external writer changing durable state while
            # the effect is in flight; the executor must detect it on reread.
            with sqlite3.connect(self.session.control_store_path) as connection:
                connection.execute(
                    "UPDATE barrier_session SET status='ambiguous' WHERE project_id=?",
                    (PROJECT,),
                )
            raise RuntimeError("injected control mutation")

        with (
            patch.object(self.adapter, "backup_bound", side_effect=mutate_then_fail),
            self.assertRaisesRegex(SQLiteAuthorityError, "durable lifecycle state changed"),
        ):
            executor.backup(self.root / "backup.sqlite", {})
        self.assertNotEqual(before.control, executor.snapshot().control)
        self.assertFalse((self.root / "backup.sqlite").exists())
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bound_lifecycle_executor_rejects_failed_effect_control_disappearance(self) -> None:
        journal = self.root / "engine-journal.json"
        journal.write_text('{"status":"running","phase":"backup","records":[]}\n', encoding="utf-8")
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        executor.snapshot()

        def delete_then_fail(_destination: Path, _binding: dict[str, Any]) -> dict[str, Any]:
            # Simulate loss of the durable session while an effect is in flight.
            with sqlite3.connect(self.session.control_store_path) as connection:
                connection.execute("DELETE FROM barrier_session WHERE project_id=?", (PROJECT,))
            raise RuntimeError("injected control disappearance")

        with (
            patch.object(self.adapter, "backup_bound", side_effect=delete_then_fail),
            self.assertRaisesRegex(SQLiteAuthorityError, "lifecycle effect failed"),
        ):
            executor.backup(self.root / "backup.sqlite", {})
        self.assertIsNone(self.session.snapshot())
        self.assertFalse((self.root / "backup.sqlite").exists())
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_unbound_lifecycle_executor_cannot_snapshot_durable_state(self) -> None:
        executor = self.adapter.lifecycle_executor()
        with self.assertRaisesRegex(SQLiteAuthorityError, "not bound to durable state"):
            executor.snapshot()
        self.assertEqual(b"clean", self.authority.read_bytes()[-5:])
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bound_lifecycle_executor_releases_lock_on_journal_failure(self) -> None:
        journal = self.root / "engine-journal.json"
        journal.write_text("not-json\n", encoding="utf-8")
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        with self.assertRaisesRegex(SQLiteAuthorityError, "durable lifecycle snapshot failed"):
            executor.snapshot()
        self.assertFalse(self.session.operation_owned_by_current_thread)
        self.assertEqual("held", self.session.snapshot().status)  # type: ignore[union-attr]

    def test_bound_lifecycle_executor_stability_reread_rejects_journal_drift(self) -> None:
        journal = self.root / "engine-journal.json"
        journal.write_text('{"status":"running","phase":"backup","records":[]}\n', encoding="utf-8")
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        baseline = executor.snapshot()
        baseline = executor.assert_snapshot_stable(baseline)
        journal.write_text('{"status":"failed","phase":"backup","records":[]}\n', encoding="utf-8")
        with self.assertRaisesRegex(SQLiteAuthorityError, "durable lifecycle state changed"):
            executor.assert_snapshot_stable(baseline)
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bound_lifecycle_executor_rejects_foreign_snapshot_baseline(self) -> None:
        journal = self.root / "engine-journal.json"
        journal.write_text('{"status":"running","phase":"backup","records":[]}\n', encoding="utf-8")
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        foreign_executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        baseline = executor.snapshot()
        with self.assertRaisesRegex(SQLiteAuthorityError, "foreign executor"):
            foreign_executor.assert_snapshot_stable(baseline)

    def test_bound_lifecycle_executor_stability_reread_rejects_control_revision_drift(self) -> None:
        journal = self.root / "engine-journal.json"
        journal.write_text('{"status":"running","phase":"backup","records":[]}\n', encoding="utf-8")
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        baseline = executor.snapshot()
        self.session.mark_ambiguous(1, "injected-failure")
        with self.assertRaisesRegex(SQLiteAuthorityError, "durable lifecycle state changed"):
            executor.assert_snapshot_stable(baseline)
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bound_lifecycle_executor_stability_reread_rejects_authority_replacement(self) -> None:
        journal = self.root / "engine-journal.json"
        journal.write_text('{"status":"running","phase":"backup","records":[]}\n', encoding="utf-8")
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        baseline = executor.snapshot()
        original = self.root / "authority-original.sqlite"
        foreign = self.root / "authority-foreign.sqlite"
        self.authority.rename(original)
        try:
            foreign.write_bytes(b"foreign authority content")
            foreign.chmod(0o600)
            foreign.rename(self.authority)
            with self.assertRaisesRegex(SQLiteAuthorityError, "authority identity changed"):
                executor.assert_snapshot_stable(baseline)
        finally:
            self.authority.unlink(missing_ok=True)
            original.rename(self.authority)
        self.assertEqual(b"clean", self.authority.read_bytes()[-5:])
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bound_lifecycle_executor_stability_reread_rejects_journal_disappearance(self) -> None:
        journal = self.root / "engine-journal.json"
        journal.write_text('{"status":"running","phase":"backup","records":[]}\n', encoding="utf-8")
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        baseline = executor.snapshot()
        journal.unlink()
        with self.assertRaisesRegex(SQLiteAuthorityError, "lifecycle journal is unavailable"):
            executor.assert_snapshot_stable(baseline)
        self.assertFalse(self.session.operation_owned_by_current_thread)
        self.assertEqual("held", self.session.snapshot().status)  # type: ignore[union-attr]

    def test_bound_lifecycle_executor_rejects_same_bytes_journal_replacement(self) -> None:
        journal = self.root / "engine-journal.json"
        content = '{"status":"running","phase":"backup","records":[]}\n'
        journal.write_text(content, encoding="utf-8")
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        baseline = executor.snapshot()
        replacement = self.root / "replacement-journal.json"
        replacement.write_text(content, encoding="utf-8")
        replacement.replace(journal)
        with self.assertRaisesRegex(SQLiteAuthorityError, "durable lifecycle state changed"):
            executor.assert_snapshot_stable(baseline)
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bound_lifecycle_executor_rejects_journal_symlink_substitution(self) -> None:
        journal = self.root / "engine-journal.json"
        content = '{"status":"running","phase":"backup","records":[]}\n'
        journal.write_text(content, encoding="utf-8")
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        baseline = executor.snapshot()
        foreign = self.root / "foreign-journal.json"
        foreign.write_text(content, encoding="utf-8")
        journal.unlink()
        journal.symlink_to(foreign)
        with self.assertRaisesRegex(SQLiteAuthorityError, "not private and regular"):
            executor.assert_snapshot_stable(baseline)
        journal.unlink()
        foreign.rename(journal)
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bound_lifecycle_executor_rejects_shared_journal_hardlink(self) -> None:
        journal = self.root / "engine-journal.json"
        content = '{"status":"running","phase":"backup","records":[]}\n'
        journal.write_text(content, encoding="utf-8")
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        baseline = executor.snapshot()
        shared = self.root / "shared-journal.json"
        os.link(journal, shared)
        with self.assertRaisesRegex(SQLiteAuthorityError, "not private and regular"):
            executor.assert_snapshot_stable(baseline)
        shared.unlink()
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bound_lifecycle_executor_rejects_journal_parent_symlink(self) -> None:
        journal_parent = self.root / "journal-root"
        journal_parent.mkdir()
        journal = journal_parent / "engine-journal.json"
        content = '{"status":"running","phase":"backup","records":[]}\n'
        journal.write_text(content, encoding="utf-8")
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        baseline = executor.snapshot()
        foreign_parent = self.root / "foreign-journal-root"
        foreign_parent.mkdir()
        (foreign_parent / journal.name).write_text(content, encoding="utf-8")
        original_parent = self.root / "journal-root-original"
        journal_parent.rename(original_parent)
        journal_parent.symlink_to(foreign_parent, target_is_directory=True)
        try:
            with self.assertRaisesRegex(SQLiteAuthorityError, "parent is not a directory"):
                executor.assert_snapshot_stable(baseline)
        finally:
            journal_parent.unlink()
            original_parent.rename(journal_parent)
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bound_lifecycle_executor_rejects_journal_parent_replacement(self) -> None:
        journal_parent = self.root / "journal-root"
        journal_parent.mkdir()
        journal = journal_parent / "engine-journal.json"
        content = '{"status":"running","phase":"backup","records":[]}\n'
        journal.write_text(content, encoding="utf-8")
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        baseline = executor.snapshot()
        foreign_parent = self.root / "foreign-journal-root"
        foreign_parent.mkdir()
        (foreign_parent / journal.name).write_text(content, encoding="utf-8")
        original_parent = self.root / "journal-root-original"
        journal_parent.rename(original_parent)
        foreign_parent.rename(journal_parent)
        try:
            with self.assertRaisesRegex(SQLiteAuthorityError, "durable lifecycle state changed"):
                executor.assert_snapshot_stable(baseline)
        finally:
            journal_parent.rename(foreign_parent)
            original_parent.rename(journal_parent)
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bound_lifecycle_executor_rejects_invalid_journal_record_shape(self) -> None:
        journal = self.root / "engine-journal.json"
        journal.write_text('{"status":"running","phase":"backup","records":{}}\n', encoding="utf-8")
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        with self.assertRaisesRegex(SQLiteAuthorityError, "durable lifecycle snapshot failed"):
            executor.snapshot()
        self.assertFalse(self.session.operation_owned_by_current_thread)
        self.assertEqual("held", self.session.snapshot().status)  # type: ignore[union-attr]

    def test_bound_lifecycle_executor_rejects_missing_initial_journal(self) -> None:
        journal = self.root / "missing-engine-journal.json"
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        with self.assertRaisesRegex(SQLiteAuthorityError, "lifecycle journal is unavailable"):
            executor.snapshot()
        self.assertFalse(self.session.operation_owned_by_current_thread)
        self.assertEqual("held", self.session.snapshot().status)  # type: ignore[union-attr]

    def test_bound_lifecycle_executor_rejects_initial_journal_symlink(self) -> None:
        journal = self.root / "engine-journal.json"
        foreign = self.root / "foreign-engine-journal.json"
        foreign.write_text(
            '{"status":"running","phase":"backup","records":[]}\n',
            encoding="utf-8",
        )
        journal.symlink_to(foreign)
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        with self.assertRaisesRegex(SQLiteAuthorityError, "not private and regular"):
            executor.snapshot()
        self.assertFalse(self.session.operation_owned_by_current_thread)
        self.assertEqual("held", self.session.snapshot().status)  # type: ignore[union-attr]

    def test_bound_lifecycle_executor_rejects_missing_journal_parent(self) -> None:
        journal = self.root / "missing-journal-parent" / "engine-journal.json"
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        with self.assertRaisesRegex(SQLiteAuthorityError, "lifecycle journal is unavailable"):
            executor.snapshot()
        self.assertFalse(journal.parent.exists())
        self.assertFalse(self.session.operation_owned_by_current_thread)
        self.assertEqual("held", self.session.snapshot().status)  # type: ignore[union-attr]

    def test_bound_lifecycle_executor_rejects_file_journal_parent(self) -> None:
        parent = self.root / "journal-parent-file"
        parent.write_bytes(b"not a directory")
        journal = parent / "engine-journal.json"
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        with self.assertRaisesRegex(SQLiteAuthorityError, "lifecycle journal is unavailable"):
            executor.snapshot()
        self.assertFalse(self.session.operation_owned_by_current_thread)
        self.assertEqual("held", self.session.snapshot().status)  # type: ignore[union-attr]

    def test_bound_lifecycle_executor_rejects_missing_control_session(self) -> None:
        journal = self.root / "engine-journal.json"
        journal.write_text('{"status":"running","phase":"backup","records":[]}\n', encoding="utf-8")
        missing_session = SQLiteBarrierSessionStore(
            SQLiteRollbackControlStore(
                self.root / "missing-control.sqlite", PROJECT, self.authority
            ),
            lambda: "authority",
        )
        executor = self.adapter.bind_lifecycle_executor(missing_session, journal)
        with self.assertRaisesRegex(SQLiteAuthorityError, "durable lifecycle snapshot failed"):
            executor.snapshot()
        self.assertFalse(missing_session.operation_owned_by_current_thread)

    def test_bound_lifecycle_executor_rejects_reused_stale_baseline(self) -> None:
        journal = self.root / "engine-journal.json"
        journal.write_text('{"status":"running","phase":"backup","records":[]}\n', encoding="utf-8")
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        first = executor.snapshot()
        second = executor.assert_snapshot_stable(first)
        self.assertIsNot(first, second)
        with self.assertRaisesRegex(SQLiteAuthorityError, "foreign executor"):
            executor.assert_snapshot_stable(first)
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bound_lifecycle_executor_rejects_authority_drift_on_final_reread(self) -> None:
        journal = self.root / "engine-journal.json"
        journal.write_text('{"status":"running","phase":"backup","records":[]}\n', encoding="utf-8")
        executor = self.adapter.bind_lifecycle_executor(self.session, journal)
        with (
            patch.object(
                self.adapter,
                "_check_identity",
                side_effect=[None, SQLiteAuthorityError("authority identity changed")],
            ),
            self.assertRaisesRegex(SQLiteAuthorityError, "authority identity changed"),
        ):
            executor.snapshot()
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bound_lifecycle_executor_rejects_foreign_authority_store(self) -> None:
        foreign_authority = self.root / "foreign-authority.sqlite"
        with sqlite3.connect(foreign_authority) as connection:
            connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY)")
        foreign_authority.chmod(0o600)
        foreign_control = self.root / "foreign-control.sqlite"
        foreign_store = SQLiteBarrierSessionStore(
            SQLiteRollbackControlStore(foreign_control, PROJECT, foreign_authority),
            lambda: "authority",
        )
        with self.assertRaisesRegex(SQLiteAuthorityError, "foreign authority"):
            self.adapter.bind_lifecycle_executor(foreign_store, self.root / "journal.json")

    def test_bound_lifecycle_executor_rejects_store_without_authority_binding(self) -> None:
        unbound_store = SQLiteBarrierSessionStore(
            SQLiteRollbackControlStore(self.root / "unbound-control.sqlite", PROJECT),
            lambda: "authority",
        )
        with self.assertRaisesRegex(SQLiteAuthorityError, "foreign authority"):
            self.adapter.bind_lifecycle_executor(unbound_store, self.root / "journal.json")

    def test_direct_lifecycle_executor_constructor_rejects_foreign_store(self) -> None:
        foreign_authority = self.root / "foreign-authority-direct.sqlite"
        with sqlite3.connect(foreign_authority) as connection:
            connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY)")
        foreign_authority.chmod(0o600)
        foreign_store = SQLiteBarrierSessionStore(
            SQLiteRollbackControlStore(
                self.root / "foreign-control-direct.sqlite", PROJECT, foreign_authority
            ),
            lambda: "authority",
        )
        with self.assertRaisesRegex(SQLiteAuthorityError, "foreign authority"):
            SQLiteLifecycleExecutor(self.adapter, foreign_store, self.root / "journal.json")

    def test_engine_bound_rollback_inspection_uses_real_scope_and_preserves_journal(self) -> None:
        context = {**CONTEXT, "operation_id": "op-real-sqlite-inspection", "project_id": PROJECT}
        artifact_root = self.root / "artifacts"
        artifact_root.mkdir()
        context.update(
            {
                "artifact_root": str(artifact_root),
                "destination": str(artifact_root / "destination"),
                "manifest": str(artifact_root / "manifest.json"),
            }
        )
        context["barrier_identity_digest"] = canonical_barrier_digest(context)
        context["envelope_digest"] = canonical_envelope_digest(context)
        engine = UpgradeEngine(
            str(context["operation_id"]),
            self.root / "engine-journal.json",
            context,
            backend_adapter=self.adapter,
            rollback_bound_verifier=BoundRollbackCapability.bind(
                PhaseContext(**cast(dict[str, Any], context)),
                self.adapter,
                self.scope,
                lease=self.lease,
                admission_recheck=self.recheck,
            ),
        )
        engine.plan()
        before = (self.root / "engine-journal.json").read_bytes()
        result = engine.inspect_rollback_bound()
        self.assertFalse(result["rollback_context_verified"])
        self.assertEqual(before, (self.root / "engine-journal.json").read_bytes())
        self.assertFalse(self.session.operation_owned_by_current_thread)
        with engine._exclusive():
            pass

    def test_clean_snapshot_is_integrity_bound_and_nonmutating(self) -> None:
        result = self.adapter.snapshot("discover", CONTEXT)
        self.assertTrue(result["sqlite_integrity_verified"])
        self.assertTrue(result["sqlite_foreign_keys_verified"])
        self.assertFalse(result["mutates_authority"])
        for phase in ("", "unknown", 1, True):
            with (
                self.subTest(phase=phase),
                self.assertRaisesRegex(SQLiteAuthorityError, "phase is invalid"),
            ):
                self.adapter.snapshot(cast(Any, phase), CONTEXT)
        durable_before = self._durable_state()
        with (
            patch.object(
                self.adapter, "snapshot", side_effect=AssertionError("SQLite reached")
            ) as snapshot,
            patch.object(
                self.adapter, "execute", side_effect=AssertionError("execute reached")
            ) as execute,
            self.assertRaisesRegex(SQLiteAuthorityError, "rollback context target"),
        ):
            self.adapter.verify_rollback_context(CONTEXT)
        snapshot.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(durable_before, self._durable_state())
        self.assertFalse(self.session.operation_owned_by_current_thread)
        rollback_context = {**CONTEXT, "target": "rollback"}
        self.assertFalse(
            self.adapter.verify_rollback_context(rollback_context)["rollback_context_verified"]
        )
        bound = self.adapter.verify_rollback_context_bound(
            {**rollback_context, "project_id": PROJECT},
            self.scope,
            lease=self.lease,
            admission_recheck=self.recheck,
        )
        self.assertEqual("rollback", bound["phase"])
        self.assertFalse(bound["rollback_context_verified"])
        self.assertFalse(self.session.operation_owned_by_current_thread)
        durable_before_bound_reject = self._durable_state()
        with (
            patch.object(
                self.adapter, "snapshot", side_effect=AssertionError("SQLite reached")
            ) as snapshot,
            patch.object(
                self.adapter, "execute", side_effect=AssertionError("execute reached")
            ) as execute,
            self.assertRaisesRegex(SQLiteAuthorityError, "rollback context target"),
        ):
            self.adapter.verify_rollback_context_bound(
                CONTEXT,
                self.scope,
                lease=self.lease,
                admission_recheck=self.recheck,
            )
        snapshot.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(durable_before_bound_reject, self._durable_state())
        self.assertFalse(self.session.operation_owned_by_current_thread)
        with self.assertRaisesRegex(SQLiteAuthorityError, "not implemented"):
            self.adapter.execute("commit", CONTEXT)
        self.assertFalse(
            self.adapter.verify_rollback_context({**CONTEXT, "target": "rollback"})[
                "rollback_context_verified"
            ]
        )

    def test_scoped_wrapper_guards_disabled_execute(self) -> None:
        adapter = ScopedBackendAdapter(self.adapter, Scope())
        self.assertTrue(adapter.snapshot("discover", CONTEXT)["sqlite_integrity_verified"])
        with self.assertRaisesRegex(TypeError, "disabled"):
            adapter.execute("commit", CONTEXT)

    def test_snapshot_bound_uses_real_scope_and_releases_after_read(self) -> None:
        result = self.adapter.snapshot_bound(
            "discover",
            {**CONTEXT, "project_id": PROJECT},
            self.scope,
            lease=self.lease,
            admission_recheck=self.recheck,
        )
        self.assertTrue(result["sqlite_integrity_verified"])
        self.assertFalse(result["mutates_authority"])
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_fresh_workers_reject_stale_and_accept_replacement_before_sqlite(self) -> None:
        ambiguous = self.session.mark_ambiguous(1, "worker-recovery")
        replacement_record = self._session_identity().as_record()
        replacement_record.update(
            {
                "attempt_id": "attempt-replacement",
                "state_revision": 2,
                "durable_barrier_id": "barrier-new",
                "fencing_token": "fence-new",
                "fencing_owner": "owner-new",
            }
        )
        replacement_record["identity_digest"] = canonical_barrier_session_digest(replacement_record)
        replacement = BarrierSessionState(
            BarrierSessionIdentity.from_record(replacement_record), "held", 1
        )
        self.session.reconcile_ambiguous(ambiguous.revision, replacement)
        before = self._durable_state()
        context = multiprocessing.get_context("fork")
        stale_result = context.Queue()
        stale = context.Process(
            target=_bound_snapshot_worker,
            args=(str(self.root), "stale", stale_result),
        )
        stale.start()
        stale.join(5)
        self.assertEqual(0, stale.exitcode)
        self.assertEqual(
            ("rejected", "SQLiteAuthorityError", 0, False), stale_result.get(timeout=1)
        )
        replacement_result = context.Queue()
        fresh = context.Process(
            target=_bound_snapshot_worker,
            args=(str(self.root), "replacement", replacement_result),
        )
        fresh.start()
        fresh.join(5)
        self.assertEqual(0, fresh.exitcode)
        self.assertEqual(("success", True, 1, False), replacement_result.get(timeout=1))
        self.assertEqual(before, self._durable_state())
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_process_abort_inside_bound_scope_releases_locks_for_fresh_worker(self) -> None:
        ambiguous = self.session.mark_ambiguous(1, "process-abort")
        replacement_record = self._session_identity().as_record()
        replacement_record.update(
            {
                "attempt_id": "attempt-abort-replacement",
                "state_revision": 2,
                "durable_barrier_id": "barrier-new",
                "fencing_token": "fence-new",
                "fencing_owner": "owner-new",
            }
        )
        replacement_record["identity_digest"] = canonical_barrier_session_digest(replacement_record)
        self.session.reconcile_ambiguous(
            ambiguous.revision,
            BarrierSessionState(BarrierSessionIdentity.from_record(replacement_record), "held", 1),
        )
        before = self._durable_state()
        context = multiprocessing.get_context("fork")
        crashed_result = context.Queue()
        crashed = context.Process(
            target=_bound_snapshot_worker,
            args=(str(self.root), "crash", crashed_result, True),
        )
        crashed.start()
        crashed.join(5)
        self.assertEqual(17, crashed.exitcode)
        self.assertFalse(self.session.operation_owned_by_current_thread)
        fresh_result = context.Queue()
        fresh = context.Process(
            target=_bound_snapshot_worker,
            args=(str(self.root), "replacement", fresh_result, True),
        )
        fresh.start()
        fresh.join(5)
        self.assertEqual(0, fresh.exitcode)
        self.assertEqual(("success", True, 1, False), fresh_result.get(timeout=1))
        self.assertEqual(before, self._durable_state())

    def test_snapshot_bound_rejects_invalid_admission_inputs_before_scope(self) -> None:
        observed = {**CONTEXT, "project_id": PROJECT}

        class CountingAdapter(SQLiteAuthorityAdapter):
            calls = 0

            def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, object]:
                self.calls += 1
                return super().snapshot(phase, context)

        adapter = CountingAdapter(self.authority)
        bound = cast(Any, adapter.snapshot_bound)
        common = {
            "phase": "discover",
            "context": observed,
            "scope": self.scope,
            "lease": self.lease,
            "admission_recheck": self.recheck,
        }
        cases = (
            ("phase", "", "phase is invalid"),
            ("phase", "unknown", "phase is invalid"),
            ("phase", 1, "phase is invalid"),
            ("phase", True, "phase is invalid"),
            ("lease", cast(Any, object()), "trusted admission lease"),
            ("admission_recheck", cast(Any, object()), "trusted admission recheck"),
            ("scope", cast(Any, object()), "concrete lock-domain scope"),
            ("context", {**observed, "backend": "git"}, "mismatched"),
        )
        for field, value, message in cases:
            with (
                self.subTest(field=field),
                patch.object(adapter, "execute", side_effect=AssertionError("execute reached")),
                self.assertRaisesRegex(SQLiteAuthorityError, message),
            ):
                bound(**{**common, field: value})
        mismatched = validate_recheck(
            AdmissionLease(PROJECT, "authority", "other", "owner", "barrier", 1),
            project_id=PROJECT,
            authority_revision="authority",
            fencing_token="other",  # noqa: S106
            fencing_owner="owner",
            durable_barrier_id="barrier",
            revision=1,
        )
        with (
            patch.object(adapter, "execute", side_effect=AssertionError("execute reached")),
            self.assertRaisesRegex(SQLiteAuthorityError, "does not match"),
        ):
            bound(**{**common, "admission_recheck": mismatched})
        stale = {**observed, "state_revision": 2}
        with (
            patch.object(adapter, "execute", side_effect=AssertionError("execute reached")),
            self.assertRaisesRegex(SQLiteAuthorityError, "trusted session identity"),
        ):
            bound(**{**common, "context": stale})
        replaced = AdmissionLease(PROJECT, "authority", "fence-new", "owner-new", "barrier-new", 2)
        replaced_recheck = validate_recheck(
            replaced,
            project_id=PROJECT,
            authority_revision="authority",
            fencing_token="fence-new",  # noqa: S106
            fencing_owner="owner-new",
            durable_barrier_id="barrier-new",
            revision=2,
        )
        with (
            patch.object(adapter, "execute", side_effect=AssertionError("execute reached")),
            self.assertRaisesRegex(SQLiteAuthorityError, "trusted session identity"),
        ):
            bound(**{**common, "lease": replaced, "admission_recheck": replaced_recheck})
        self.assertEqual(0, adapter.calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_snapshot_bound_rejects_each_untrusted_sqlite_result_claim(self) -> None:
        class TamperedAdapter(SQLiteAuthorityAdapter):
            field = "backend_identity_verified"
            replacement: object = False

            def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, object]:
                value = super().snapshot(phase, context)
                value[self.field] = self.replacement
                return value

        for field, replacement, message in (
            ("backend_identity_verified", False, "backend identity is unverified"),
            ("sqlite_integrity_verified", False, "integrity is unverified"),
            ("sqlite_foreign_keys_verified", False, "foreign keys are unverified"),
            ("mutates_authority", True, "not read-only"),
        ):
            with self.subTest(field=field):
                adapter = type(
                    "FieldTamperedAdapter",
                    (TamperedAdapter,),
                    {"field": field, "replacement": replacement},
                )(self.authority)
                before = self._durable_state()
                with (
                    patch.object(adapter, "execute", side_effect=AssertionError("execute reached")),
                    self.assertRaisesRegex(SQLiteAuthorityError, message),
                ):
                    adapter.snapshot_bound(
                        "discover",
                        {**CONTEXT, "project_id": PROJECT},
                        self.scope,
                        lease=self.lease,
                        admission_recheck=self.recheck,
                    )
                self.assertEqual(before, self._durable_state())
                self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_snapshot_bound_rejects_backend_equivalence_drift_without_execute(self) -> None:
        class TamperedAdapter(SQLiteAuthorityAdapter):
            def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, object]:
                value = super().snapshot(phase, context)
                value["project_id"] = "tampered"
                return value

        tampered = TamperedAdapter(self.authority)
        before = self._durable_state()
        with (
            patch.object(tampered, "execute", side_effect=AssertionError("execute reached")),
            self.assertRaisesRegex(SQLiteAuthorityError, "backend context identity"),
        ):
            tampered.snapshot_bound(
                "discover",
                {**CONTEXT, "project_id": PROJECT},
                self.scope,
                lease=self.lease,
                admission_recheck=self.recheck,
            )
        self.assertFalse(self.session.operation_owned_by_current_thread)
        self.assertEqual(before, self._durable_state())

    def test_snapshot_bound_rejects_non_read_only_result(self) -> None:
        class TamperedAdapter(SQLiteAuthorityAdapter):
            def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, object]:
                value = super().snapshot(phase, context)
                value["mutates_authority"] = True
                return value

        tampered = TamperedAdapter(self.authority)
        before = self._durable_state()
        with (
            patch.object(tampered, "execute", side_effect=AssertionError("execute reached")),
            self.assertRaisesRegex(SQLiteAuthorityError, "not read-only"),
        ):
            tampered.snapshot_bound(
                "discover",
                {**CONTEXT, "project_id": PROJECT},
                self.scope,
                lease=self.lease,
                admission_recheck=self.recheck,
            )
        self.assertFalse(self.session.operation_owned_by_current_thread)
        self.assertEqual(before, self._durable_state())

    def test_snapshot_bound_rejects_phase_drift_without_execute(self) -> None:
        class TamperedAdapter(SQLiteAuthorityAdapter):
            def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, object]:
                value = super().snapshot(phase, context)
                value["phase"] = "other"
                return value

        tampered = TamperedAdapter(self.authority)
        before = self._durable_state()
        with (
            patch.object(tampered, "execute", side_effect=AssertionError("execute reached")),
            self.assertRaisesRegex(SQLiteAuthorityError, "backend phase changed"),
        ):
            tampered.snapshot_bound(
                "discover",
                {**CONTEXT, "project_id": PROJECT},
                self.scope,
                lease=self.lease,
                admission_recheck=self.recheck,
            )
        self.assertFalse(self.session.operation_owned_by_current_thread)
        self.assertEqual(before, self._durable_state())

    def test_snapshot_bound_requires_exact_result_schema_and_types(self) -> None:
        fields = {
            "phase": 1,
            "backend_identity_verified": 1,
            "sqlite_integrity_verified": 1,
            "sqlite_foreign_keys_verified": 1,
            "mutates_authority": 0,
        }

        class TamperedAdapter(SQLiteAuthorityAdapter):
            mode = "wrong"
            field = "phase"
            wrong: object = 1

            def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, object]:
                value = super().snapshot(phase, context)
                if self.mode == "missing":
                    value.pop(self.field)
                elif self.mode == "extra":
                    value["unexpected"] = True
                else:
                    value[self.field] = self.wrong
                return value

        for mode in ("missing", "extra", "wrong"):
            for field, wrong in fields.items():
                with self.subTest(mode=mode, field=field):
                    adapter = type(
                        "SchemaTamperedAdapter",
                        (TamperedAdapter,),
                        {"mode": mode, "field": field, "wrong": wrong},
                    )(self.authority)
                    before = self._durable_state()
                    with (
                        patch.object(
                            adapter, "execute", side_effect=AssertionError("execute reached")
                        ),
                        self.assertRaises(SQLiteAuthorityError),
                    ):
                        adapter.snapshot_bound(
                            "discover",
                            {**CONTEXT, "project_id": PROJECT},
                            self.scope,
                            lease=self.lease,
                            admission_recheck=self.recheck,
                        )
                    self.assertFalse(self.session.operation_owned_by_current_thread)
                    self.assertEqual(before, self._durable_state())

    def test_corrupt_or_mismatched_authority_fails_closed(self) -> None:
        self.authority.write_bytes(b"not sqlite")
        with self.assertRaisesRegex(SQLiteAuthorityError, "observation failed"):
            self.adapter.snapshot("discover", CONTEXT)
        with self.assertRaisesRegex(SQLiteAuthorityError, "mismatched"):
            self.adapter.snapshot("discover", {**CONTEXT, "backend": "git"})
        with self.assertRaisesRegex(SQLiteAuthorityError, "context types"):
            self.adapter.snapshot("discover", {**CONTEXT, "operation_id": 1})

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

    def test_exclusive_transaction_failure_then_process_death_recovers(self) -> None:
        context = multiprocessing.get_context("fork")
        ready, release = context.Pipe(duplex=True)
        worker = context.Process(
            target=_hold_exclusive_transaction,
            args=(str(self.authority), release),
        )
        worker.start()
        self.assertTrue(ready.recv())
        try:
            with self.assertRaisesRegex(SQLiteAuthorityError, "observation failed"):
                self.adapter.snapshot("discover", CONTEXT)
        finally:
            worker.terminate()
            worker.join(5)
            ready.close()
            release.close()
        self.assertFalse(worker.is_alive())
        self.assertTrue(
            SQLiteAuthorityAdapter(self.authority).snapshot("discover", CONTEXT)[
                "sqlite_integrity_verified"
            ]
        )
        with sqlite3.connect(self.authority) as connection:
            self.assertEqual("clean", connection.execute("SELECT body FROM records").fetchone()[0])

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

    def test_bound_rollback_rejects_stale_and_replaced_sessions_before_sqlite(self) -> None:
        rollback = {**CONTEXT, "project_id": PROJECT, "target": "rollback"}
        ambiguous = self.session.mark_ambiguous(1, "rollback-stale")
        before_stale = self._durable_state()
        with (
            patch.object(
                self.adapter, "snapshot", side_effect=AssertionError("SQLite reached")
            ) as snapshot,
            patch.object(
                self.adapter, "execute", side_effect=AssertionError("execute reached")
            ) as execute,
            self.assertRaisesRegex(SQLiteAuthorityError, "trusted SQLite session"),
        ):
            self.adapter.verify_rollback_context_bound(
                rollback,
                self.scope,
                lease=self.lease,
                admission_recheck=self.recheck,
            )
        snapshot.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(before_stale, self._durable_state())
        self.assertFalse(self.session.operation_owned_by_current_thread)

        replacement_record = self._session_identity().as_record()
        replacement_record.update(
            {
                "attempt_id": "rollback-replacement",
                "state_revision": 2,
                "durable_barrier_id": "barrier-replacement",
                "fencing_token": "fence-replacement",
                "fencing_owner": "owner-replacement",
            }
        )
        replacement_record["identity_digest"] = canonical_barrier_session_digest(replacement_record)
        replacement = BarrierSessionState(
            BarrierSessionIdentity.from_record(replacement_record), "held", 1
        )
        self.session.reconcile_ambiguous(ambiguous.revision, replacement)
        before_replaced = self._durable_state()
        with (
            patch.object(
                self.adapter, "snapshot", side_effect=AssertionError("SQLite reached")
            ) as snapshot,
            patch.object(
                self.adapter, "execute", side_effect=AssertionError("execute reached")
            ) as execute,
            self.assertRaisesRegex(SQLiteAuthorityError, "trusted SQLite session"),
        ):
            self.adapter.verify_rollback_context_bound(
                rollback,
                self.scope,
                lease=self.lease,
                admission_recheck=self.recheck,
            )
        snapshot.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(before_replaced, self._durable_state())
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bound_rollback_rejects_tampered_sqlite_results_without_state_change(self) -> None:
        rollback = {**CONTEXT, "project_id": PROJECT, "target": "rollback"}

        class TamperedAdapter(SQLiteAuthorityAdapter):
            field = "phase"
            replacement: object = "discover"

            def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, object]:
                value = super().snapshot(phase, context)
                value[self.field] = self.replacement
                return value

        fields = (
            ("phase", "discover", "backend phase changed"),
            ("project_id", "tampered", "backend context identity changed"),
            ("backend_identity_verified", False, "identity is unverified"),
            ("sqlite_integrity_verified", False, "integrity is unverified"),
            ("sqlite_foreign_keys_verified", False, "foreign keys are unverified"),
            ("mutates_authority", True, "not read-only"),
        )
        for field, replacement, message in fields:
            with self.subTest(field=field):
                adapter = type(
                    "RollbackTamperedSQLiteAdapter",
                    (TamperedAdapter,),
                    {"field": field, "replacement": replacement},
                )(self.authority)
                before = self._durable_state()
                with (
                    patch.object(
                        adapter, "execute", side_effect=AssertionError("execute reached")
                    ) as execute,
                    self.assertRaisesRegex(SQLiteAuthorityError, message),
                ):
                    adapter.verify_rollback_context_bound(
                        rollback,
                        self.scope,
                        lease=self.lease,
                        admission_recheck=self.recheck,
                    )
                execute.assert_not_called()
                self.assertEqual(before, self._durable_state())
                self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bound_rollback_rejects_authority_and_descriptor_drift_without_execute(self) -> None:
        rollback = {**CONTEXT, "project_id": PROJECT, "target": "rollback"}
        drifted = AdmissionLease(PROJECT, "authority-drift", "fence", "owner", "barrier", 1)
        drifted_recheck = validate_recheck(
            drifted,
            project_id=PROJECT,
            authority_revision="authority-drift",
            fencing_token="fence",  # noqa: S106
            fencing_owner="owner",
            durable_barrier_id="barrier",
            revision=1,
        )
        before = self._durable_state()
        with (
            patch.object(
                self.adapter, "snapshot", side_effect=AssertionError("SQLite reached")
            ) as snapshot,
            patch.object(
                self.adapter, "execute", side_effect=AssertionError("execute reached")
            ) as execute,
            self.assertRaisesRegex(SQLiteAuthorityError, "trusted SQLite session reread"),
        ):
            self.adapter.verify_rollback_context_bound(
                {**rollback, "authority_revision": "authority-drift"},
                self.scope,
                lease=drifted,
                admission_recheck=drifted_recheck,
            )
        snapshot.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(before, self._durable_state())
        self.assertFalse(self.session.operation_owned_by_current_thread)

        replacement = self.root / "rollback-authority-replacement.sqlite"
        replacement.write_bytes(self.authority.read_bytes())
        replacement.chmod(0o600)
        before_descriptor = (
            self.authority.read_bytes(),
            self.session.control_store_path.read_bytes(),
        )
        replacement.replace(self.authority)
        with (
            patch.object(
                self.adapter, "execute", side_effect=AssertionError("execute reached")
            ) as execute,
            self.assertRaisesRegex(SQLiteAuthorityError, "trusted SQLite session reread"),
        ):
            self.adapter.verify_rollback_context_bound(
                rollback,
                self.scope,
                lease=self.lease,
                admission_recheck=self.recheck,
            )
        execute.assert_not_called()
        self.assertEqual(
            before_descriptor,
            (self.authority.read_bytes(), self.session.control_store_path.read_bytes()),
        )
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bound_rollback_rejects_malformed_admission_before_sqlite(self) -> None:
        common = {
            "context": {**CONTEXT, "project_id": PROJECT, "target": "rollback"},
            "scope": self.scope,
            "lease": self.lease,
            "admission_recheck": self.recheck,
        }
        bound = cast(Any, self.adapter.verify_rollback_context_bound)
        cases = (
            ("lease", cast(Any, object()), "trusted admission lease"),
            ("admission_recheck", cast(Any, object()), "trusted admission recheck"),
            ("scope", cast(Any, object()), "concrete lock-domain scope"),
            (
                "context",
                {**CONTEXT, "project_id": PROJECT, "target": "unexpected"},
                "rollback context target",
            ),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                before = self._durable_state()
                with (
                    patch.object(
                        self.adapter, "snapshot", side_effect=AssertionError("SQLite reached")
                    ) as snapshot,
                    patch.object(
                        self.adapter, "execute", side_effect=AssertionError("execute reached")
                    ) as execute,
                    self.assertRaisesRegex(SQLiteAuthorityError, message),
                ):
                    bound(**{**common, field: value})
                snapshot.assert_not_called()
                execute.assert_not_called()
                self.assertEqual(before, self._durable_state())
                self.assertFalse(self.session.operation_owned_by_current_thread)


if __name__ == "__main__":
    unittest.main()
