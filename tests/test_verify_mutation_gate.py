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
        self.assertIn("implementation_refinement_not_proven", decision["reasons"])

    def test_partial_or_malformed_contracts_fail_closed(self) -> None:
        with self.assertRaisesRegex(MutationGateError, "boundary"):
            evaluate_mutation_gate({})
        with self.assertRaisesRegex(MutationGateError, "correspondence"):
            evaluate_mutation_gate({"refinement_boundary": {"implementation_refinement": "proven"}})
        with self.assertRaisesRegex(MutationGateError, "entry 0"):
            evaluate_mutation_gate(
                {
                    "refinement_boundary": {"implementation_refinement": "proven"},
                    "correspondence": [None],
                }
            )

    def test_policy_and_each_unproven_entry_keep_gate_denied(self) -> None:
        contract = {
            "refinement_boundary": {"implementation_refinement": "proven"},
            "correspondence": [{"status": "proven"}, {"status": "pending"}],
            "mutation_gate": "unexpected",
        }
        decision = evaluate_mutation_gate(contract)
        self.assertEqual("deny", decision["decision"])
        self.assertIn("correspondence_1_not_proven", decision["reasons"])
        self.assertIn("mutation_gate_policy_not_enabled", decision["reasons"])


if __name__ == "__main__":
    unittest.main()
