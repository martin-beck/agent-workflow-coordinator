# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile tests for the integrated mutation-free rehearsal."""

from __future__ import annotations

import unittest
from typing import cast

from tools.authority_neutral_rehearsal import (
    PHASE_ORDER,
    BoundIntegratedRehearsal,
    RehearsalExecutionError,
    execute_integrated_rehearsal,
)


def _records() -> list[dict[str, object]]:
    base = {
        "backend": "sqlite",
        "operation_id": "op-1",
        "fencing_token": "fence-1",
        "mutates_authority": False,
        "outcome": "completed",
    }
    proofs = {
        "backup": {"backup_verified": True, "restore_roundtrip_verified": True},
        "stage": {"staged_verified": True, "manifest_verified": True},
        "validate": {"runtime_validated": True},
        "selector_admission": {"selector_admission_verified": True},
        "runtime_admission": {"runtime_replacement_admission_verified": True},
        "commit_admission": {"commit_prerequisites_verified": True},
        "recovery_admission": {"recovery_evidence_verified": True},
    }
    return [{"phase": phase, **base, **proofs[phase]} for phase in PHASE_ORDER]


class IntegratedRehearsalTests(unittest.TestCase):
    def test_complete_trace_is_functional_and_mutation_free(self) -> None:
        result = execute_integrated_rehearsal(_records())
        self.assertEqual(list(PHASE_ORDER), result["phases_verified"])
        self.assertTrue(result["functional_available"])
        self.assertTrue(result["write_closed"])
        self.assertFalse(result["mutation_dispatched"])

    def test_rejects_order_identity_and_mutation_drift(self) -> None:
        records = _records()
        records[1], records[2] = records[2], records[1]
        with self.assertRaisesRegex(RehearsalExecutionError, "stage"):
            execute_integrated_rehearsal(records)
        records = _records()
        records[4]["fencing_token"] = "foreign"  # noqa: S105
        with self.assertRaisesRegex(RehearsalExecutionError, "identity drifted"):
            execute_integrated_rehearsal(records)
        records = _records()
        records[5]["mutates_authority"] = True
        with self.assertRaisesRegex(RehearsalExecutionError, "mutating"):
            execute_integrated_rehearsal(records)
        records = _records()
        records[2]["outcome"] = "unknown"
        with self.assertRaisesRegex(RehearsalExecutionError, "outcome is invalid"):
            execute_integrated_rehearsal(records)
        records = _records()
        records[2].update(outcome="failed", functional_available=False, write_closed=True)
        with self.assertRaisesRegex(RehearsalExecutionError, "failure is unsafe"):
            execute_integrated_rehearsal(records)
        records = _records()
        records[2].pop("runtime_validated")
        with self.assertRaisesRegex(RehearsalExecutionError, "lacks runtime_validated"):
            execute_integrated_rehearsal(records)

    def test_failure_paths_require_functional_write_closed_recovery(self) -> None:
        records = _records()
        records[2].update(
            outcome="failed",
            functional_available=True,
            write_closed=True,
        )
        result = execute_integrated_rehearsal(records)
        self.assertFalse(result["mutation_dispatched"])
        records = _records()
        records[3].update(outcome="ambiguous", functional_available=True, write_closed=True)
        with self.assertRaisesRegex(RehearsalExecutionError, "unreconciled"):
            execute_integrated_rehearsal(records)
        records[3]["reconciliation_required"] = True
        self.assertTrue(execute_integrated_rehearsal(records)["reconciliation_required"])

    def test_rejects_invalid_trace_shapes_and_identity(self) -> None:
        with self.assertRaisesRegex(RehearsalExecutionError, "records are invalid"):
            execute_integrated_rehearsal(cast(object, None))  # type: ignore[arg-type]
        records = _records()
        with self.assertRaisesRegex(RehearsalExecutionError, "trace is incomplete"):
            execute_integrated_rehearsal(records[:-1])
        records[0] = cast(dict[str, object], None)
        with self.assertRaisesRegex(RehearsalExecutionError, "identity is unavailable"):
            execute_integrated_rehearsal(records)
        records = _records()
        records[0]["operation_id"] = ""
        with self.assertRaisesRegex(RehearsalExecutionError, "identity is invalid"):
            execute_integrated_rehearsal(records)

    def test_bound_rehearsal_rejects_trace_identity_drift(self) -> None:
        records = _records()
        bound = BoundIntegratedRehearsal(records)
        self.assertFalse(bound.execute(records)["mutation_dispatched"])
        records[0]["operation_id"] = "foreign"
        with self.assertRaisesRegex(RehearsalExecutionError, "identity drifted"):
            bound.execute(records)
        bound = BoundIntegratedRehearsal(_records())
        bound._identity["operation_id"] = "foreign"
        with self.assertRaisesRegex(RehearsalExecutionError, "identity mismatch"):
            bound.execute(_records())


if __name__ == "__main__":
    unittest.main()
