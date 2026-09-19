# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile validation tests for the disabled durable session contract."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from tools.durable_session_contract import (
    DurableSessionContractError,
    InMemoryJournalRecordStore,
    classify_journal_recovery,
    deserialize_journal_record,
    load_contract,
    reconcile_journal_envelopes,
    reconcile_journal_records,
    recovery_decision,
    serialize_journal_record,
    validate_contract,
    validate_journal_record,
    validate_journal_transaction,
    validate_outcome,
    validate_recovery_decision,
    validate_recovery_outcome,
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

    def test_journal_envelope_is_canonical_and_identity_bound(self) -> None:
        expected = snapshot()
        record = {
            "operation_id": "op-1",
            "state_revision": 3,
            "journal_identity": "journal:1",
            "status": "captured",
            "fsync": "durable",
        }
        payload = serialize_journal_record(record, expected)
        self.assertEqual(payload, serialize_journal_record(dict(record), expected))
        self.assertEqual(record, deserialize_journal_record(payload, expected))
        with self.assertRaisesRegex(DurableSessionContractError, "malformed"):
            deserialize_journal_record(payload[:-1], expected)
        with self.assertRaisesRegex(DurableSessionContractError, "identity"):
            deserialize_journal_record(payload, {**expected, "journal_identity": "foreign"})
        for forged in (
            {**json.loads(payload), "kind": "foreign"},
            {**json.loads(payload), "schema_version": 2},
            {**json.loads(payload), "extra": True},
            {**json.loads(payload), "record": []},
        ):
            with self.assertRaises(DurableSessionContractError):
                deserialize_journal_record(json.dumps(forged, sort_keys=True).encode(), expected)
        for invalid in (bytearray(payload), "text"):
            with self.assertRaisesRegex(DurableSessionContractError, "bytes"):
                deserialize_journal_record(invalid, expected)  # type: ignore[arg-type]

    def test_envelope_sequence_reconciles_idempotent_history_and_fences_drift(self) -> None:
        expected = snapshot()
        record = {
            "operation_id": "op-1",
            "state_revision": 3,
            "journal_identity": "journal:1",
            "status": "captured",
            "fsync": "durable",
        }
        payload = serialize_journal_record(record, expected)
        self.assertEqual(record, reconcile_journal_envelopes((payload, payload), expected))
        with self.assertRaisesRegex(DurableSessionContractError, "missing"):
            reconcile_journal_envelopes((), expected)
        with self.assertRaisesRegex(DurableSessionContractError, "malformed"):
            reconcile_journal_envelopes((payload, payload[:-1]), expected)
        ambiguous = serialize_journal_record(
            {**record, "status": "ambiguous", "fsync": "uncertain"}, expected
        )
        with self.assertRaisesRegex(DurableSessionContractError, "safe mode"):
            reconcile_journal_envelopes((payload, ambiguous), expected)

    def test_transaction_requires_contiguous_states_and_terminal_finality(self) -> None:
        expected = snapshot()

        def state(sequence: int, value: str = "captured") -> dict[str, object]:
            return {
                "operation_id": "op-1",
                "sequence": sequence,
                "intent": "append" if sequence == 0 else "replay",
                "state": value,
                "state_revision": 3,
                "journal_identity": "journal:1",
                "fsync": "durable",
            }

        valid = (state(0), state(1), state(2, "terminal"))
        self.assertEqual(valid, validate_journal_transaction(valid, expected))
        cases = (
            ((state(1),), "sequence"),
            ((state(0), {**state(1), "state": "terminal"}, state(2)), "continues"),
            (({**state(0), "intent": "unknown"},), "intent"),
            (({**state(0), "state": "unknown"},), "state is invalid"),
            (({key: value for key, value in state(0).items() if key != "fsync"},), "fields"),
            (({**state(0), "state_revision": 4},), "revision"),
            (({**state(0), "journal_identity": "foreign"},), "identity"),
            (({**state(0), "state": "ambiguous", "fsync": "uncertain"},), "safe mode"),
            (({**state(0), "fsync": "uncertain"},), "safe mode"),
            (({**state(0), "fsync": "lost"},), "fsync is invalid"),
        )
        for records, message in cases:
            with self.assertRaisesRegex(DurableSessionContractError, message):
                validate_journal_transaction(records, expected)
        with self.assertRaisesRegex(DurableSessionContractError, "missing"):
            validate_journal_transaction((), expected)

    def test_recovery_classification_is_idempotent_and_fail_closed(self) -> None:
        expected = snapshot()

        def state(sequence: int, value: str = "captured") -> dict[str, object]:
            return {
                "operation_id": "op-1",
                "sequence": sequence,
                "intent": "append" if sequence == 0 else "replay",
                "state": value,
                "state_revision": 3,
                "journal_identity": "journal:1",
                "fsync": "durable",
            }

        self.assertEqual("resume", classify_journal_recovery((state(0),), expected))
        self.assertEqual(
            "terminal", classify_journal_recovery((state(0), state(1, "terminal")), expected)
        )
        with self.assertRaisesRegex(DurableSessionContractError, "safe mode"):
            classify_journal_recovery((state(0), state(1, "rollback_required")), expected)

    def test_recovery_decision_is_idempotent_and_explicitly_fences_rollback(self) -> None:
        expected = snapshot()

        def state(sequence: int, value: str = "captured") -> dict[str, object]:
            return {
                "operation_id": "op-1",
                "sequence": sequence,
                "intent": "append" if sequence == 0 else "replay",
                "state": value,
                "state_revision": 3,
                "journal_identity": "journal:1",
                "fsync": "durable",
            }

        resumed = recovery_decision((state(0),), expected)
        self.assertEqual("resume", resumed["decision"])
        self.assertFalse(resumed["safe_mode"])
        self.assertEqual(resumed, recovery_decision((state(0),), expected))
        terminal = recovery_decision((state(0), state(1, "terminal")), expected)
        self.assertEqual(
            {"decision": "terminal", "safe_mode": False, "rollback_required": False},
            {key: terminal[key] for key in ("decision", "safe_mode", "rollback_required")},
        )
        rollback = recovery_decision((state(0), state(1, "rollback_required")), expected)
        self.assertEqual("rollback_required", rollback["decision"])
        self.assertTrue(rollback["safe_mode"])
        self.assertTrue(rollback["rollback_required"])

    def test_recovery_decision_provenance_and_monotonicity_are_fail_closed(self) -> None:
        expected = snapshot()

        def decision(value: str) -> dict[str, object]:
            rollback = value == "rollback_required"
            return {
                "operation_id": "op-1",
                "state_revision": 3,
                "journal_identity": "journal:1",
                "decision": value,
                "safe_mode": rollback,
                "rollback_required": rollback,
            }

        validate_recovery_decision(decision("resume"), expected)
        expected_with_operation = {**expected, "operation_id": "op-1"}
        with self.assertRaisesRegex(DurableSessionContractError, "operation"):
            validate_recovery_decision(
                {**decision("resume"), "operation_id": "foreign"}, expected_with_operation
            )
        validate_recovery_decision(decision("terminal"), expected, decision("resume"))
        validate_recovery_decision(decision("rollback_required"), expected, decision("resume"))
        validate_recovery_decision(decision("terminal"), expected, decision("terminal"))
        for forged, message in (
            (
                {key: value for key, value in decision("resume").items() if key != "decision"},
                "fields",
            ),
            ({**decision("resume"), "journal_identity": "foreign"}, "identity"),
            ({**decision("resume"), "decision": "other"}, "invalid"),
            ({**decision("resume"), "safe_mode": True}, "safe-mode"),
            ({**decision("resume"), "rollback_required": True}, "rollback"),
            ({**decision("resume"), "state_revision": 4}, "revision"),
        ):
            with self.assertRaisesRegex(DurableSessionContractError, message):
                validate_recovery_decision(forged, expected)
        with self.assertRaisesRegex(DurableSessionContractError, "monotonic"):
            validate_recovery_decision(decision("resume"), expected, decision("terminal"))
        with self.assertRaisesRegex(DurableSessionContractError, "previous"):
            validate_recovery_decision(decision("resume"), expected, {"decision": "resume"})

    def test_recovery_outcome_is_identity_bound_and_terminal_only(self) -> None:
        expected = snapshot()
        terminal = {
            "operation_id": "op-1",
            "state_revision": 3,
            "journal_identity": "journal:1",
            "decision": "terminal",
            "safe_mode": False,
            "rollback_required": False,
        }
        outcome = {
            "operation_id": "op-1",
            "state_revision": 3,
            "journal_identity": "journal:1",
            "outcome": "success",
            "decision": "terminal",
        }
        validate_recovery_outcome(outcome, terminal, expected)
        validate_recovery_outcome({**outcome, "outcome": "rejected"}, terminal, expected)
        rollback = {
            **terminal,
            "decision": "rollback_required",
            "safe_mode": True,
            "rollback_required": True,
        }
        validate_recovery_outcome(
            {**outcome, "outcome": "ambiguous", "decision": "rollback_required"}, rollback, expected
        )
        cases = (
            (
                {key: value for key, value in outcome.items() if key != "decision"},
                terminal,
                "fields",
            ),
            ({**outcome, "operation_id": "foreign"}, terminal, "operation"),
            ({**outcome, "state_revision": 4}, terminal, "revision"),
            ({**outcome, "journal_identity": "foreign"}, terminal, "identity"),
            ({**outcome, "decision": "rollback_required"}, rollback, "terminal"),
            ({**outcome, "outcome": "ambiguous"}, terminal, "rollback"),
            ({**outcome, "decision": "resume"}, terminal, "inconsistent"),
            ({**outcome, "outcome": "unknown"}, terminal, "invalid"),
        )
        for forged, decision, message in cases:
            with self.assertRaisesRegex(DurableSessionContractError, message):
                validate_recovery_outcome(forged, decision, expected)

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
