# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
import unittest

from tools.durable_session_chain import (
    FIELDS,
    DurableSessionChain,
    SessionChainError,
    canonical_record_digest,
    reconcile_replay,
    validate_chain,
    validate_record,
)


def record(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "session_id": "session-1",
        "operation_id": "upgrade-1",
        "parent_operation_id": None,
        "journal_sequence": 1,
        "previous_record_digest": None,
        "state_revision": 1,
        "journal_identity": "journal-generation-1",
        "authority_identity": "authority-1",
        "barrier_id": "barrier-1",
        "fencing_owner": "owner-1",
        "fencing_token": "fence-1",
        "status": "open",
        "fsync_state": "durable",
        "payload": {"phase": "discover"},
    }
    value.update(changes)
    value["record_digest"] = canonical_record_digest({**value, "record_digest": "0" * 64})
    return value


class DurableSessionChainTests(unittest.TestCase):
    def test_valid_chain_and_continuation(self) -> None:
        first = record()
        second = record(
            journal_sequence=2,
            previous_record_digest=first["record_digest"],
            state_revision=2,
            payload={"phase": "preflight"},
        )
        self.assertEqual((first, second), validate_chain([first, second]))

    def test_rejects_gaps_and_broken_hash_chain(self) -> None:
        first = record()
        with self.assertRaises(SessionChainError):
            validate_chain(
                [first, record(journal_sequence=3, previous_record_digest=first["record_digest"])]
            )
        with self.assertRaises(SessionChainError):
            validate_chain([first, record(journal_sequence=2, previous_record_digest="0" * 64)])

    def test_rejects_identity_replacement_and_tamper(self) -> None:
        first = record()
        second = record(
            journal_sequence=2,
            previous_record_digest=first["record_digest"],
            session_id="other-session",
        )
        with self.assertRaises(SessionChainError):
            validate_chain([first, second])
        tampered = dict(first)
        tampered["payload"] = {"phase": "changed"}
        with self.assertRaises(SessionChainError):
            validate_record(tampered)

    def test_exact_replay_only(self) -> None:
        first = record()
        self.assertEqual("exact", reconcile_replay(first, dict(first)))
        conflicting = dict(first)
        conflicting["payload"] = {"phase": "other"}
        conflicting["record_digest"] = canonical_record_digest(conflicting)
        with self.assertRaises(SessionChainError):
            reconcile_replay(first, conflicting)

    def test_rejects_uncertain_fsync_and_invalid_shape(self) -> None:
        with self.assertRaises(SessionChainError):
            validate_record(record(fsync_state="uncertain"))
        with self.assertRaises(SessionChainError):
            validate_record({field: record()[field] for field in FIELDS[:-1]})

    def test_rejects_all_record_validation_boundaries(self) -> None:
        with self.assertRaises(SessionChainError):
            canonical_record_digest({})
        malformed_json = record()
        malformed_json["payload"] = {"bad": object()}
        with self.assertRaises(SessionChainError):
            canonical_record_digest(malformed_json)
        with self.assertRaises(SessionChainError):
            validate_record([])  # type: ignore[arg-type]
        for field, value in (
            ("schema_version", 2),
            ("session_id", "bad value"),
            ("operation_id", "bad value"),
            ("parent_operation_id", "bad value"),
            ("journal_identity", "bad value"),
            ("authority_identity", "bad value"),
            ("barrier_id", "bad value"),
            ("fencing_owner", "bad value"),
            ("fencing_token", "bad value"),
            ("journal_sequence", 0),
            ("state_revision", True),
            ("previous_record_digest", "not-a-digest"),
            ("status", "unknown"),
            ("fsync_state", "unknown"),
            ("payload", []),
            ("record_digest", "not-a-digest"),
        ):
            candidate = record()
            candidate[field] = value
            if field not in {"record_digest", "payload"}:
                candidate["record_digest"] = canonical_record_digest(candidate)
            with self.subTest(field=field), self.assertRaises(SessionChainError):
                validate_record(candidate)

    def test_rejects_genesis_identity_and_replay_conflicts(self) -> None:
        first = record(previous_record_digest="0" * 64)
        with self.assertRaises(SessionChainError):
            validate_chain([first])
        first = record()
        second = record(
            journal_sequence=2,
            previous_record_digest=first["record_digest"],
            state_revision=2,
        )
        for field in (
            "session_id",
            "journal_identity",
            "authority_identity",
            "barrier_id",
            "fencing_owner",
            "fencing_token",
        ):
            changed = dict(second)
            changed[field] = "replacement"
            changed["record_digest"] = canonical_record_digest(changed)
            with self.subTest(field=field), self.assertRaises(SessionChainError):
                validate_chain([first, changed])
        with self.assertRaises(SessionChainError):
            reconcile_replay(first, record(journal_sequence=2))

    def test_chain_wrapper_exposes_terminal_and_append_without_persistence(self) -> None:
        empty = DurableSessionChain.load([])
        self.assertIsNone(empty.terminal)
        first = record()
        chain = DurableSessionChain.load([first])
        self.assertEqual(first, chain.terminal)
        second = record(
            journal_sequence=2,
            previous_record_digest=first["record_digest"],
            state_revision=2,
        )
        self.assertEqual(second, chain.append(second).terminal)


if __name__ == "__main__":
    unittest.main()
