# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile validation tests for the disabled durable session contract."""

from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path

from tools.durable_session_contract import (
    DurableSessionContractError,
    InMemoryJournalRecordStore,
    load_contract,
    reconcile_journal_records,
    validate_contract,
    validate_journal_record,
    validate_outcome,
    validate_snapshot,
)


def snapshot() -> dict[str, object]:
    return {
        "authority_identity": "authority:1",
        "control_store_identity": "control:1",
        "control_lock_identity": "lock:1",
        "journal_identity": "journal:1",
        "journal_bytes": "digest:1",
        "barrier_id": "barrier-1",
        "fencing_owner": "owner-1",
        "fencing_token": "token-1",
        "state_revision": 3,
    }


class DurableSessionContractTests(unittest.TestCase):
    def test_checked_in_contract_is_disabled_and_valid(self) -> None:
        contract = load_contract()
        self.assertFalse(contract["mutation_enabled"])
        self.assertFalse(contract["dispatch_enabled"])

    def test_snapshot_requires_exact_identity_and_revision(self) -> None:
        current = snapshot()
        validate_snapshot(current, dict(current))
        foreign = dict(current, authority_identity="authority:foreign")
        with self.assertRaisesRegex(DurableSessionContractError, "authority_identity"):
            validate_snapshot(current, foreign)
        with self.assertRaises(DurableSessionContractError):
            validate_snapshot(
                {key: value for key, value in current.items() if key != "journal_bytes"}, current
            )

    def test_outcome_rejects_replay_foreign_and_unknown_records(self) -> None:
        expected = snapshot()
        record = {
            "operation_id": "op-1",
            "opcode": "backend.backup",
            "outcome": "success",
            "state_revision": 3,
            "identity_digest": "journal:1",
        }
        validate_outcome(record, expected)
        with self.assertRaisesRegex(DurableSessionContractError, "stale"):
            validate_outcome(dict(record, state_revision=2), expected)
        with self.assertRaisesRegex(DurableSessionContractError, "foreign"):
            validate_outcome(dict(record, identity_digest="journal:foreign"), expected)
        with self.assertRaisesRegex(DurableSessionContractError, "outcome value"):
            validate_outcome(dict(record, outcome="replayed"), expected)

    def test_journal_record_requires_atomic_durable_or_ambiguous_pair(self) -> None:
        expected = snapshot()
        record = {
            "operation_id": "op-1",
            "state_revision": 3,
            "journal_identity": "journal:1",
            "status": "captured",
            "fsync": "durable",
        }
        validate_journal_record(record, expected)
        validate_journal_record({**record, "status": "ambiguous", "fsync": "uncertain"}, expected)
        for malformed in (
            {**record, "status": "ambiguous"},
            {**record, "fsync": "uncertain"},
            {**record, "fsync": "lost"},
            {**record, "status": "lost"},
            {**record, "journal_identity": "foreign"},
            {**record, "state_revision": 2},
            {key: value for key, value in record.items() if key != "operation_id"},
            {**record, "operation_id": ""},
        ):
            with self.assertRaises(DurableSessionContractError):
                validate_journal_record(malformed, expected)
        with self.assertRaisesRegex(DurableSessionContractError, "expected session"):
            validate_journal_record(record, {"journal_identity": "journal:1"})

    def test_reconcile_accepts_idempotent_reads_and_rejects_uncertainty(self) -> None:
        expected = snapshot()
        stable = {
            "operation_id": "op-1",
            "state_revision": 3,
            "journal_identity": "journal:1",
            "status": "captured",
            "fsync": "durable",
        }
        self.assertEqual(stable, reconcile_journal_records((stable, dict(stable)), expected))
        with self.assertRaisesRegex(DurableSessionContractError, "missing"):
            reconcile_journal_records((), expected)
        with self.assertRaisesRegex(DurableSessionContractError, "changed"):
            reconcile_journal_records((stable, {**stable, "operation_id": "replayed"}), expected)
        with self.assertRaisesRegex(DurableSessionContractError, "safe mode"):
            reconcile_journal_records(
                (stable, {**stable, "status": "ambiguous", "fsync": "uncertain"}), expected
            )

    def test_in_memory_store_is_idempotent_and_fences_replay_or_ambiguity(self) -> None:
        expected = snapshot()
        record = {
            "operation_id": "op-1",
            "state_revision": 3,
            "journal_identity": "journal:1",
            "status": "captured",
            "fsync": "durable",
        }
        store = InMemoryJournalRecordStore(expected)
        self.assertEqual(record, store.append(record))
        self.assertEqual(record, store.append(dict(record)))
        self.assertEqual((record,), store.records())
        observed = InMemoryJournalRecordStore(expected)
        self.assertEqual(record, observed.observe(record))
        key = ("op-1", 3, "journal:1")
        observed._records[key] = {**record, "operation_id": "tampered"}
        with self.assertRaisesRegex(DurableSessionContractError, "replay"):
            observed.append(record)
        self.assertTrue(observed.safe_mode)
        with self.assertRaisesRegex(DurableSessionContractError, "durable capture"):
            store.append({**record, "status": "ambiguous", "fsync": "uncertain"})
        self.assertTrue(store.safe_mode)
        with self.assertRaisesRegex(DurableSessionContractError, "safe mode"):
            store.observe(record)
        with self.assertRaisesRegex(DurableSessionContractError, "safe mode"):
            store.append(record)

    def test_in_memory_store_rejects_incomplete_expected_identity(self) -> None:
        with self.assertRaisesRegex(DurableSessionContractError, "expected session"):
            InMemoryJournalRecordStore({"journal_identity": "journal:1"})

    def test_in_memory_store_fences_uncertain_observation_and_foreign_identity(self) -> None:
        expected = snapshot()
        record = {
            "operation_id": "op-1",
            "state_revision": 3,
            "journal_identity": "journal:1",
            "status": "captured",
            "fsync": "durable",
        }
        store = InMemoryJournalRecordStore(expected)
        with self.assertRaisesRegex(DurableSessionContractError, "foreign"):
            store.observe({**record, "journal_identity": "journal:replacement"})
        self.assertFalse(store.safe_mode)
        with self.assertRaisesRegex(DurableSessionContractError, "safe mode"):
            store.observe({**record, "status": "ambiguous", "fsync": "uncertain"})
        self.assertTrue(store.safe_mode)

    def test_contract_drift_cannot_enable_mutation_or_dispatch(self) -> None:
        contract = load_contract()
        for field in ("mutation_enabled", "dispatch_enabled"):
            forged = copy.deepcopy(contract)
            forged[field] = True
            with self.assertRaisesRegex(DurableSessionContractError, "disabled"):
                validate_contract(forged)

    def test_contract_rejects_malformed_artifacts_and_schema_drift(self) -> None:
        valid = load_contract()
        cases = [
            ("kind", {**valid, "kind": "foreign"}, "kind"),
            ("outcome", {**valid, "outcome_publication": "enabled"}, "publication"),
            ("ambiguous", {**valid, "ambiguous_observation": "continue"}, "ambiguous"),
            ("capture", {**valid, "capture": None}, "missing"),
            ("fields", {**valid, "capture": ["only"]}, "incomplete"),
            ("atomic", {**valid, "outcome_record": {"atomic": False}}, "atomic"),
            (
                "required",
                {
                    **valid,
                    "outcome_record": {
                        "atomic": True,
                        "required": [],
                        "allowed": list({"success", "rejected", "ambiguous"}),
                    },
                },
                "fields",
            ),
            (
                "allowed",
                {
                    **valid,
                    "outcome_record": {
                        "atomic": True,
                        "required": list(
                            {
                                "operation_id",
                                "opcode",
                                "outcome",
                                "state_revision",
                                "identity_digest",
                            }
                        ),
                        "allowed": [],
                    },
                },
                "outcomes",
            ),
            ("journal-atomic", {**valid, "journal_record": {"atomic": False}}, "journal"),
            (
                "journal-fields",
                {**valid, "journal_record": {"atomic": True, "required": []}},
                "journal",
            ),
            (
                "journal-status",
                {**valid, "journal_record": {**valid["journal_record"], "allowed_status": []}},
                "statuses",
            ),
            (
                "journal-fsync",
                {**valid, "journal_record": {**valid["journal_record"], "allowed_fsync": []}},
                "fsync",
            ),
        ]
        for name, value, message in cases:
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(DurableSessionContractError, message),
            ):
                validate_contract(value)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(DurableSessionContractError, "object"):
                load_contract(path)
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(DurableSessionContractError, "unavailable"):
                load_contract(path)
            with self.assertRaisesRegex(DurableSessionContractError, "unavailable"):
                load_contract(Path(directory) / "missing.json")

    def test_snapshot_and_outcome_reject_invalid_types_and_shapes(self) -> None:
        current = snapshot()
        for field, value in (("state_revision", -1), ("state_revision", True), ("barrier_id", "")):
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(DurableSessionContractError),
            ):
                validate_snapshot(dict(current, **{field: value}), current)
        with self.assertRaises(DurableSessionContractError):
            validate_snapshot(current, {**current, "state_revision": "3"})
        record = {
            "operation_id": "op-1",
            "opcode": "backend.backup",
            "outcome": "success",
            "state_revision": 3,
            "identity_digest": "journal:1",
        }
        for malformed in (
            {key: value for key, value in record.items() if key != "opcode"},
            {**record, "operation_id": ""},
            {**record, "opcode": ""},
        ):
            with self.assertRaises(DurableSessionContractError):
                validate_outcome(malformed, current)
        with self.assertRaises(DurableSessionContractError):
            validate_outcome(record, {**current, "state_revision": 4})
        with self.assertRaises(DurableSessionContractError):
            validate_outcome(
                record, {key: value for key, value in current.items() if key != "journal_identity"}
            )

    def test_process_death_cannot_authorize_outcome_publication(self) -> None:
        child = os.fork()
        if child == 0:  # pragma: no cover - executed in the forked process
            load_contract()
            os._exit(23)
        _, status = os.waitpid(child, 0)
        self.assertEqual(23, os.waitstatus_to_exitcode(status))
        self.assertEqual("disabled", load_contract()["outcome_publication"])


if __name__ == "__main__":
    unittest.main()
