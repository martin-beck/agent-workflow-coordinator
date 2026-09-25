# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Fail-closed tests for mutation enablement decisions."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tools.verify_mutation_gate import MutationGateError, evaluate_mutation_gate

CONTRACT = Path(__file__).resolve().parents[1] / "formal/upgrade/v10-refinement-contract.json"


class MutationGateTests(unittest.TestCase):
    def test_current_contract_denies_mutation_without_dispatch(self) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        decision = evaluate_mutation_gate(contract)
        self.assertEqual("deny", decision["decision"])
        self.assertFalse(decision["mutation_enabled"])
        self.assertFalse(decision["authority_mutation_dispatched"])
        self.assertNotIn("implementation_refinement_not_proven", decision["reasons"])
        self.assertNotIn("operational_obligation_0_incomplete", decision["reasons"])
        self.assertIn("mutation_gate_policy_rejection_only", decision["reasons"])

    def test_partial_or_malformed_contracts_fail_closed(self) -> None:
        with self.assertRaisesRegex(MutationGateError, "boundary"):
            evaluate_mutation_gate({})
        with self.assertRaisesRegex(MutationGateError, "correspondence"):
            evaluate_mutation_gate(
                {"refinement_boundary": {"implementation_refinement": "not-required"}}
            )
        with self.assertRaisesRegex(MutationGateError, "entry 0"):
            evaluate_mutation_gate(
                {
                    "refinement_boundary": {"implementation_refinement": "not-required"},
                    "correspondence": [None],
                }
            )

    def test_policy_and_missing_operational_evidence_keep_gate_denied(self) -> None:
        contract = {
            "refinement_boundary": {"implementation_refinement": "not-required"},
            "correspondence": [
                {"status": "evidence-complete", "evidence_required": ["test_a"]},
                {"status": "best-effort", "evidence_required": []},
            ],
            "mutation_gate": "unexpected",
        }
        decision = evaluate_mutation_gate(contract)
        self.assertEqual("deny", decision["decision"])
        self.assertIn("operational_obligation_1_incomplete", decision["reasons"])
        self.assertIn("mutation_gate_policy_not_enabled", decision["reasons"])

    def test_best_effort_correspondence_status_does_not_require_refinement_proof(self) -> None:
        contract = {
            "refinement_boundary": {"implementation_refinement": "not-required"},
            "correspondence": [
                {
                    "status": "bounded executable evidence; best-effort model correspondence",
                    "evidence_required": ["tests/test_gate.py::test_evidence"],
                }
            ],
            "mutation_gate": "upgrade apply remains rejection-only",
        }
        decision = evaluate_mutation_gate(contract)
        self.assertEqual(["mutation_gate_policy_rejection_only"], decision["reasons"])
        self.assertFalse(decision["mutation_enabled"])


if __name__ == "__main__":
    unittest.main()
