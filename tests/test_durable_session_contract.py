# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile validation tests for the disabled durable session contract."""

from __future__ import annotations

import copy
import os
import unittest

from tools.durable_session_contract import (
    DurableSessionContractError,
    load_contract,
    validate_contract,
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

    def test_contract_drift_cannot_enable_mutation_or_dispatch(self) -> None:
        contract = load_contract()
        for field in ("mutation_enabled", "dispatch_enabled"):
            forged = copy.deepcopy(contract)
            forged[field] = True
            with self.assertRaisesRegex(DurableSessionContractError, "disabled"):
                validate_contract(forged)

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
