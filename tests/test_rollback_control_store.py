# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile and durability tests for the SQLite rollback control store."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tools.rollback_control_store import (
    IDENTITY_FIELDS,
    ControlStoreError,
    SQLiteControlStoreAdapter,
    SQLiteRollbackControlStore,
    bind_control_store,
)
from tools.upgrade_identity import canonical_barrier_digest, canonical_envelope_digest

PROJECT = "11111111-1111-4111-8111-111111111111"
RECORD = {
    "schema_version": 2,
    "backend": "sqlite",
    "project_id": PROJECT,
    "operation_id": "op-1",
    "state_revision": 1,
    "authority_revision": "authority-1",
    "fencing_token": "fence-1",
    "fencing_owner": "owner-1",
    "durable_barrier_id": "barrier-1",
    "artifact_root": "/artifacts",
    "source": "/authority.sqlite",
    "destination": "/artifacts/backup.sqlite",
    "manifest": "/artifacts/manifest.json",
    "barrier_identity_digest": "0" * 64,
    "target": "rollback",
    "envelope_digest": "0" * 64,
    "status": "held",
    "revision": 1,
}
RECORD["barrier_identity_digest"] = canonical_barrier_digest(RECORD)
RECORD["envelope_digest"] = canonical_envelope_digest(RECORD)
RELEASE_EVIDENCE = {
    "restored_verified": True,
    "runtime_validated": True,
    "backend_roundtrip_valid": True,
    "backend": "sqlite",
    "fencing_token": "fence-1",
}


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

    def test_adapter_release_api_fails_closed_without_scope_and_authority_evidence(self) -> None:
        class MissingVerifier:
            def snapshot(self, _phase: str, _context: Mapping[str, object]) -> Mapping[str, object]:
                return {}

            def execute(self, _phase: str, _context: Mapping[str, object]) -> Mapping[str, object]:
                return {}

        class InvalidVerifier(MissingVerifier):
            def revalidate_rollback(
                self, _context: Mapping[str, object], _result: Mapping[str, object]
            ) -> object:
                return None

        class EvidenceVerifier(MissingVerifier):
            evidence: object = RELEASE_EVIDENCE

            def revalidate_rollback(
                self, _context: Mapping[str, object], _result: Mapping[str, object]
            ) -> object:
                return self.evidence

        with tempfile.TemporaryDirectory() as directory:
            authority = Path(directory) / "authority.sqlite"
            authority.touch()
            store = SQLiteRollbackControlStore(
                Path(directory) / "control.sqlite", PROJECT, authority
            )
            store.cas(0, RECORD)
            context = {field: RECORD[field] for field in IDENTITY_FIELDS}
            adapter = SQLiteControlStoreAdapter(MissingVerifier(), store)
            with self.assertRaises(ControlStoreError):
                adapter.begin_release_rollback_context(context)
            with self.assertRaises(ControlStoreError):
                adapter.complete_release_rollback_context(context)
            with self.assertRaises(ControlStoreError):
                adapter.revalidate_rollback(context, RELEASE_EVIDENCE)
            with store.operation_lock():
                adapter.begin_release_rollback_context(context)
                with self.assertRaises(ControlStoreError):
                    adapter.complete_release_rollback_context(context)
                with self.assertRaises(ControlStoreError):
                    adapter.revalidate_rollback(context, RELEASE_EVIDENCE)
                invalid = SQLiteControlStoreAdapter(InvalidVerifier(), store)
                with self.assertRaises(ControlStoreError):
                    invalid.revalidate_rollback(context, RELEASE_EVIDENCE)
            with self.assertRaises(ControlStoreError):
                SQLiteControlStoreAdapter(InvalidVerifier(), store).revalidate_rollback(
                    context, RELEASE_EVIDENCE
                )

            second_store = SQLiteRollbackControlStore(
                Path(directory) / "second-control.sqlite", PROJECT, authority
            )
            second_store.cas(0, RECORD)
            verifier = EvidenceVerifier()
            evidence_adapter = SQLiteControlStoreAdapter(verifier, second_store)
            with second_store.operation_lock():
                evidence_adapter.begin_release_rollback_context(context)
                with self.assertRaises(ControlStoreError):
                    evidence_adapter.revalidate_rollback(
                        {**context, "fencing_token": "stale"}, RELEASE_EVIDENCE
                    )
                for evidence in (
                    {**RELEASE_EVIDENCE, "runtime_validated": False},
                    {**RELEASE_EVIDENCE, "unexpected": True},
                    {**RELEASE_EVIDENCE, "fencing_token": "stale"},
                ):
                    verifier.evidence = evidence
                    with self.subTest(evidence=evidence), self.assertRaises(ControlStoreError):
                        evidence_adapter.revalidate_rollback(context, RELEASE_EVIDENCE)
                verifier.evidence = RELEASE_EVIDENCE
                self.assertEqual(
                    RELEASE_EVIDENCE,
                    evidence_adapter.revalidate_rollback(context, RELEASE_EVIDENCE),
                )
                self.assertEqual(
                    "released",
                    evidence_adapter.complete_release_rollback_context(context)["status"],
                )

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
            with store.operation_lock():
                store._begin_release_locked("op-1")
                authorization = store._authorize_release_locked(created, RELEASE_EVIDENCE)
                released = store._complete_release_locked("op-1", authorization)
            self.assertEqual("released", released["status"])
            self.assertEqual(3, store.snapshot("op-1")["revision"])
            with closing(sqlite3.connect(Path(directory) / "control.sqlite")) as connection:
                self.assertEqual("wal", connection.execute("PRAGMA journal_mode").fetchone()[0])

    def test_status_transition_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRollbackControlStore(Path(directory) / "control.sqlite", PROJECT)
            store.cas(0, RECORD)
            releasing = store.cas(1, {**RECORD, "status": "releasing", "revision": 2})
            with self.assertRaises(ControlStoreError):
                store.cas(2, {**releasing, "status": "held", "revision": 3})

    def test_released_barrier_is_terminal_and_cannot_be_made_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRollbackControlStore(Path(directory) / "control.sqlite", PROJECT)
            store.cas(0, RECORD)
            with store.operation_lock():
                store._begin_release_locked("op-1")
                authorization = store._authorize_release_locked(RECORD, RELEASE_EVIDENCE)
                released = store._complete_release_locked("op-1", authorization)
            self.assertEqual("released", released["status"])
            with self.assertRaises(ControlStoreError):
                store.cas(3, {**released, "status": "ambiguous", "revision": 4})

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
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("UPDATE control_meta SET value='99' WHERE key='schema_version'")
                connection.commit()
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

            dangling_target = root / "missing.sqlite"
            dangling = root / "dangling.sqlite"
            dangling.symlink_to(dangling_target.name)
            with self.assertRaises(ControlStoreError):
                SQLiteRollbackControlStore(dangling, PROJECT, authority)

            absent_alias = root / "absent-alias.sqlite"
            with self.assertRaises(ControlStoreError):
                SQLiteRollbackControlStore(absent_alias, PROJECT, absent_alias)

            linked_parent = root / "linked-parent"
            linked_parent.symlink_to(root)
            with self.assertRaises(ControlStoreError):
                SQLiteRollbackControlStore(linked_parent / "control.sqlite", PROJECT)

    def test_nonregular_alias_and_changed_parent_identities_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ControlStoreError):
                SQLiteRollbackControlStore(root / "missing" / "control.sqlite", PROJECT)
            directory_control = root / "directory-control"
            directory_control.mkdir()
            with self.assertRaises(ControlStoreError):
                SQLiteRollbackControlStore(directory_control, PROJECT)
            authority_directory = root / "authority-directory"
            authority_directory.mkdir()
            with self.assertRaises(ControlStoreError):
                SQLiteRollbackControlStore(root / "control.sqlite", PROJECT, authority_directory)
            authority = root / "authority.sqlite"
            authority.touch()
            alias = root / "alias.sqlite"
            os.link(authority, alias)
            with self.assertRaises(ControlStoreError):
                SQLiteRollbackControlStore(alias, PROJECT, authority)

    def test_control_path_swap_after_binding_fails_without_authority_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = root / "authority.sqlite"
            with closing(sqlite3.connect(authority)) as connection:
                connection.execute("CREATE TABLE authority_payload(value TEXT)")
                connection.commit()
            control = root / "control.sqlite"
            store = SQLiteRollbackControlStore(control, PROJECT, authority)
            original = root / "original-control.sqlite"
            control.rename(original)
            control.symlink_to(authority.name)

            with self.assertRaises(ControlStoreError):
                store.cas(0, RECORD)
            with closing(sqlite3.connect(authority)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertEqual({"authority_payload"}, tables)

    def test_control_swap_between_descriptor_validation_and_sqlite_open_fails_closed(self) -> None:
        class SwappingStore(SQLiteRollbackControlStore):
            swapped = False

            def _open_bound_file(
                self,
                path: Path,
                expected_parent: tuple[int, int],
                expected_file: tuple[int, int],
            ) -> tuple[int, int]:
                parent, descriptor = super()._open_bound_file(path, expected_parent, expected_file)
                if path == self.path and not self.swapped:
                    self.swapped = True
                    self.path.rename(self.path.with_suffix(".original"))
                    assert self.authority_path is not None
                    self.path.symlink_to(self.authority_path.name)
                return parent, descriptor

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = root / "authority.sqlite"
            with closing(sqlite3.connect(authority)) as connection:
                connection.execute("CREATE TABLE authority_payload(value TEXT)")
                connection.commit()
            store = SwappingStore(root / "control.sqlite", PROJECT, authority)

            with self.assertRaises(ControlStoreError):
                store.cas(0, RECORD)
            with closing(sqlite3.connect(authority)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertEqual({"authority_payload"}, tables)

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

    def test_release_reconciliation_requires_verified_engine_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRollbackControlStore(Path(directory) / "control.sqlite", PROJECT)
            held = store.cas(0, RECORD)
            releasing = store.cas(1, {**held, "status": "releasing", "revision": 2})
            with self.assertRaises(ControlStoreError):
                store.reconcile_release("op-1")
            self.assertEqual(2, store.snapshot("op-1")["revision"])
            self.assertEqual("releasing", releasing["status"])

    def test_with_barrier_rejects_reentrant_store_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRollbackControlStore(Path(directory) / "control.sqlite", PROJECT)

            def reenter(_record: Mapping[str, object]) -> Mapping[str, object]:
                store.snapshot("op-1")
                return RECORD

            with self.assertRaises(ControlStoreError):
                store.with_barrier(0, RECORD, reenter)
            self.assertEqual("held", store.snapshot("op-1")["status"])

    def test_operation_lock_is_single_nonreentrant_outer_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRollbackControlStore(Path(directory) / "control.sqlite", PROJECT)
            with store.operation_lock(), self.assertRaises(ControlStoreError):
                store.operation_lock().__enter__()
            with store.operation_lock(), self.assertRaises(ControlStoreError):
                store.snapshot("op-1")

    def test_operation_lock_blocks_a_second_store_instance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.sqlite"
            first = SQLiteRollbackControlStore(path, PROJECT)
            second = SQLiteRollbackControlStore(path, PROJECT)
            started = threading.Event()
            finished = threading.Event()
            errors: list[str] = []

            def read_from_second_instance() -> None:
                started.set()
                try:
                    second.snapshot("op-1")
                except ControlStoreError as error:
                    errors.append(str(error))
                finally:
                    finished.set()

            with first.operation_lock():
                worker = threading.Thread(target=read_from_second_instance)
                worker.start()
                self.assertTrue(started.wait(1))
                self.assertFalse(finished.wait(0.1))
            self.assertTrue(finished.wait(2))
            worker.join()
            self.assertEqual(["control barrier is missing"], errors)

    def test_control_lock_timeout_is_bounded_and_preserves_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.sqlite"
            first = SQLiteRollbackControlStore(path, PROJECT)
            second = SQLiteRollbackControlStore(path, PROJECT)
            with (
                first._control_lock(),
                patch(
                    "tools.rollback_control_store.time.monotonic",
                    side_effect=(0.0, 0.0, 11.0),
                ),
                patch("tools.rollback_control_store.time.sleep"),
                self.assertRaises(ControlStoreError),
            ):
                second.cas(0, RECORD)
            with self.assertRaises(ControlStoreError):
                first.snapshot("op-1")

    def test_releasing_barrier_cannot_be_completed_without_authority_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRollbackControlStore(Path(directory) / "control.sqlite", PROJECT)
            store.cas(0, RECORD)
            store.begin_release("op-1")
            with self.assertRaises(ControlStoreError):
                store.reconcile_release("op-1")
            self.assertEqual("releasing", store.snapshot("op-1")["status"])

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

    def test_cas_rejects_missing_stale_active_changed_and_illegal_transitions(self) -> None:
        def candidate(**changes: object) -> dict[str, object]:
            value = {**RECORD, **changes}
            value["barrier_identity_digest"] = canonical_barrier_digest(value)
            value["envelope_digest"] = canonical_envelope_digest(value)
            return value

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRollbackControlStore(Path(directory) / "control.sqlite", PROJECT)
            with self.assertRaises(ControlStoreError):
                store.cas(1, RECORD)
            held = store.cas(0, RECORD)
            with self.assertRaises(ControlStoreError):
                store.cas(0, candidate(operation_id="op-2", state_revision=2))
            changed_fence = candidate(revision=2)
            changed_fence["fencing_token"] = f"{RECORD['fencing_token']}-changed"
            changed_fence["barrier_identity_digest"] = canonical_barrier_digest(changed_fence)
            changed_fence["envelope_digest"] = canonical_envelope_digest(changed_fence)
            with self.assertRaises(ControlStoreError):
                store.cas(1, changed_fence)
            with self.assertRaises(ControlStoreError):
                store.cas(1, candidate(project_id="22222222-2222-4222-8222-222222222222"))
            ambiguous = store.cas(1, {**held, "status": "ambiguous", "revision": 2})
            with self.assertRaises(ControlStoreError):
                store.cas(2, {**ambiguous, "status": "held", "revision": 3})
            with self.assertRaises(ControlStoreError):
                store.cas(0, candidate(operation_id="op-stale", state_revision=1))

    def test_boolean_expected_revision_is_rejected_by_public_and_private_cas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRollbackControlStore(Path(directory) / "control.sqlite", PROJECT)
            with self.assertRaises(ControlStoreError):
                store.cas(False, RECORD)
            with self.assertRaises(ControlStoreError):
                store.snapshot("op-1")
            created = store.cas(0, RECORD)
            with store.operation_lock(), self.assertRaises(ControlStoreError):
                store._cas_locked(True, {**created, "status": "releasing", "revision": 2})
            self.assertEqual(1, store.snapshot("op-1")["revision"])

    def test_invalid_identity_and_status_are_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRollbackControlStore(Path(directory) / "control.sqlite", PROJECT)
            for mutation in (
                {"project_id": "project-1"},
                {"backend": "git"},
                {"envelope_digest": "f" * 63},
                {"operation_id": "../escape"},
                {"state_revision": False},
                {"revision": False},
                {"revision": 0},
                {"status": "unknown"},
            ):
                with self.subTest(mutation=mutation), self.assertRaises(ControlStoreError):
                    store.cas(0, {**RECORD, **mutation})
            with self.assertRaises(ControlStoreError):
                store.snapshot("op-1")


if __name__ == "__main__":
    unittest.main()
