# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Additional fail-closed coverage for small standalone validator boundaries."""

from __future__ import annotations

import json
import sys
import unittest
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest import mock

from tools import validate_upgrade_contract as upgrade_validator
from tools.formal_correspondence import formal_provenance, validate_trace
from tools.integration_contract import IntegrationError
from tools.tui_integration import TRACE_STEPS, Initiator, TuiAcceptanceTrace, TuiIntegrationError
from tools.verify_mutation_gate import MutationGateError, evaluate_mutation_gate

ROOT = Path(__file__).resolve().parents[1]


def upgrade_contract() -> dict[str, Any]:
    """Load the checked-in, valid upgrade example without mutating it."""
    value = json.loads(
        (ROOT / "examples/upgrade/fixture-v0.3.8-to-v0.3.9.contract.json").read_text(
            encoding="utf-8"
        )
    )
    assert isinstance(value, dict)
    return deepcopy(value)


def acceptance_trace(**changes: object) -> TuiAcceptanceTrace:
    """Build a complete public-safe trace, with individual hostile overrides."""
    return TuiAcceptanceTrace(
        task_id=str(changes.get("task_id", "AR-0095")),
        task_revision=cast(int, changes.get("task_revision", 1)),
        initiator=Initiator.USER,
        steps=cast(tuple[str, ...], changes.get("steps", TRACE_STEPS)),
        formal_spec_ref=str(changes.get("formal_spec_ref", "quality/formal/95")),
        user_disposition_ref=str(changes.get("user_disposition_ref", "guidance/decision/95")),
        quality_evidence_ref=str(changes.get("quality_evidence_ref", "quality/evidence/95")),
        future_ar_ref=str(changes.get("future_ar_ref", "AR-0096")),
        implementation_ref=str(changes.get("implementation_ref", "coordinator/handoff/95")),
        decision_authority=str(changes.get("decision_authority", "coordinator")),
    )


class UpgradeValidatorDefenseTests(unittest.TestCase):
    def test_schema_and_semantic_defenses_are_independent_of_runtime_screening(self) -> None:
        mutations: tuple[tuple[str, Any], ...] = (
            ("schema_version", 3),
            ("same_release", None),
            ("same_version", None),
            ("tag_reference", None),
            ("authority_phase", None),
            ("rollback_target", None),
        )
        with mock.patch.object(upgrade_validator, "validate_runtime_contract"):
            for name, replacement in mutations:
                document = upgrade_contract()
                if name == "schema_version":
                    document["schema_version"] = replacement
                elif name == "same_release":
                    document["to"] = deepcopy(document["from"])
                elif name == "same_version":
                    document["to"]["version"] = document["from"]["version"]
                elif name == "tag_reference":
                    document["from"]["tag_ref"] = "refs/tags/v9.9.9"
                elif name == "authority_phase":
                    document["phases"][0]["mutates_authority"] = True
                else:
                    document["rollback"]["operation"]["inputs"]["target"] = "continue"
                with self.subTest(name=name), self.assertRaises(upgrade_validator.ContractError):
                    upgrade_validator.validate_contract(document)

    def test_cli_uses_sys_argv_and_rejects_unreadable_contracts(self) -> None:
        with mock.patch.object(sys, "argv", ["validate_upgrade_contract.py"]):
            self.assertEqual(2, upgrade_validator.main())
        self.assertEqual(1, upgrade_validator.main(["missing-contract.json"]))

    def test_rollback_target_has_a_semantic_fail_closed_check(self) -> None:
        document = upgrade_contract()
        rollback_inputs = document["rollback"]["operation"]["inputs"]
        assert isinstance(rollback_inputs, dict)
        rollback_inputs["target"] = "continue"
        with self.assertRaisesRegex(upgrade_validator.ContractError, "rollback target"):
            upgrade_validator._validate_inputs(
                rollback_inputs,
                str(document["backend"]),
                str(document["operation_id"]),
                rollback=True,
            )


class MutationGateDefenseTests(unittest.TestCase):
    def test_exact_head_evidence_is_the_only_allowing_decision(self) -> None:
        decision = evaluate_mutation_gate(
            {
                "refinement_boundary": {"implementation_refinement": "not-required"},
                "correspondence": [{"evidence_required": ["tests/test_gate.py::test_exact"]}],
                "mutation_gate": "exact-head executable evidence recorded",
            }
        )
        self.assertEqual("allow", decision["decision"])
        self.assertTrue(decision["mutation_enabled"])
        self.assertEqual([], decision["reasons"])

    def test_non_object_and_non_list_evidence_are_rejected_or_denied(self) -> None:
        with self.assertRaisesRegex(MutationGateError, "not an object"):
            evaluate_mutation_gate(cast(Mapping[str, Any], []))
        decision = evaluate_mutation_gate(
            {
                "refinement_boundary": {"implementation_refinement": "still-required"},
                "correspondence": [{"evidence_required": "not-a-list"}],
                "mutation_gate": "exact-head executable evidence recorded",
            }
        )
        self.assertFalse(decision["mutation_enabled"])
        self.assertEqual(
            ["refinement_proof_requirement_not_removed", "operational_obligation_0_incomplete"],
            decision["reasons"],
        )


class FormalCorrespondenceDefenseTests(unittest.TestCase):
    def test_unknown_preterminal_event_and_incomplete_model_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown event"):
            validate_trace(("unknown", "backup_verified"))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "formal/handoffctl/Handoffctl.tla"
            model.parent.mkdir(parents=True)
            model.write_text("---- MODULE Handoffctl ----\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "formal invariant definitions missing"):
                formal_provenance(root)


class TuiIntegrationDefenseTests(unittest.TestCase):
    def test_invalid_identity_and_noncanonical_trace_are_rejected(self) -> None:
        with self.assertRaisesRegex(TuiIntegrationError, "identity"):
            acceptance_trace(task_id="task-95").accept()
        with self.assertRaisesRegex(TuiIntegrationError, "unknown"):
            acceptance_trace(steps=tuple(reversed(TRACE_STEPS))).accept()

    def test_private_reference_and_downstream_contract_errors_are_wrapped(self) -> None:
        with self.assertRaisesRegex(TuiIntegrationError, "public-safe"):
            acceptance_trace(implementation_ref="coordinator//private").accept()
        with (
            mock.patch(
                "tools.tui_integration.IntegrationContract",
                side_effect=IntegrationError("unsafe evidence"),
            ),
            self.assertRaisesRegex(TuiIntegrationError, "cross-project evidence is invalid"),
        ):
            acceptance_trace().accept()


if __name__ == "__main__":
    unittest.main()
