# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Tests for the correctness-first release-upgrade contract."""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import jsonschema

ROOT = Path(__file__).resolve().parents[1]

_validator_spec = importlib.util.spec_from_file_location(
    "upgrade_contract_validator", ROOT / "tools/validate_upgrade_contract.py"
)
assert _validator_spec and _validator_spec.loader
_validator = importlib.util.module_from_spec(_validator_spec)
_validator_spec.loader.exec_module(_validator)
ContractError = _validator.ContractError
validate_contract = _validator.validate_contract
validate_phases = _validator._validate_phases
validate_backends = _validator._validate_backends
validate_main = _validator.main


def contract() -> dict[str, Any]:
    phases = [
        "discover",
        "preflight",
        "quiesce",
        "backup",
        "stage",
        "commit",
        "validate",
        "reopen",
    ]
    return {
        "schema_version": 1,
        "operation_id": "upgrade:v0.3.5-to-v0.3.6:001",
        "from": {
            "version": "v0.3.5",
            "source_commit": "713761b428676c0c290b207d4f04eee0a57390c3",
            "tag_ref": "refs/tags/v0.3.5",
            "tag_object": "a" * 40,
            "signature_sha256": "b" * 64,
            "trust_policy_sha256": "c" * 64,
            "vendor_manifest_sha256": "d" * 64,
        },
        "to": {
            "version": "v0.3.6",
            "source_commit": "713761b428676c0c290b207d4f04eee0a57390c3",
            "tag_ref": "refs/tags/v0.3.6",
            "tag_object": "e" * 40,
            "signature_sha256": "f" * 64,
            "trust_policy_sha256": "0" * 64,
            "vendor_manifest_sha256": "1" * 64,
        },
        "preconditions": [
            {
                "id": "RELEASE.AUTH",
                "effect": "read-only",
                "failure_mode": "stop-before-mutation",
                "preconditions": ["source-is-immutable"],
                "postconditions": ["release-identity-recorded"],
                "evidence": ["release-manifest"],
            }
        ],
        "phases": [
            {
                "id": phase,
                "order": order,
                "mutates_authority": phase == "commit",
                "requires": (
                    []
                    if order == 1
                    else ["quiesce", "backup", "stage"]
                    if phase == "commit"
                    else ["validate"]
                    if phase == "reopen"
                    else [phases[order - 2]]
                ),
                "on_failure": "restore-known-good",
                "operation": {
                    "operation_id": f"upgrade:v0.3.5-to-v0.3.6:001:{phase}",
                    "timeout_seconds": 300,
                    "resources": ["maintenance-barrier"],
                    "preconditions": ["previous-phase-complete"],
                    "postconditions": ["phase-contract-satisfied"],
                    "evidence": ["durable-operation-record"],
                    "durable_record": "operation-id-and-outcome",
                },
            }
            for order, phase in enumerate(phases, 1)
        ],
        "backend_contracts": [
            {
                "backend": "git",
                "authority": ["reachable-objects", "refs", "index", "task-history"],
                "backup": ["object-and-ref-inventory", "binding", "projections"],
                "restore": ["verify-objects", "restore-refs", "verify-history"],
                "selector": ["coordinator.backend.json"],
                "projections": ["CURRENT.md", "STATUS.md"],
                "equivalence": "authority-compatible-round-trip",
            },
            {
                "backend": "sqlite",
                "authority": ["database", "wal", "shm"],
                "backup": ["online-backup-api", "binding", "projections"],
                "restore": ["integrity-check", "restore-selector", "verify-history"],
                "selector": ["coordinator.backend.json"],
                "projections": ["CURRENT.md", "STATUS.md"],
                "equivalence": "authority-compatible-round-trip",
                "wal": ["checkpoint-policy", "synchronous-full"],
            },
        ],
        "rollback": {
            "required": True,
            "backup_integrity": "backend-specific",
            "integrity_by_backend": {
                "git": "git-object-and-ref",
                "sqlite": "sqlite-integrity-and-backup-api",
            },
            "equivalence": "authority-compatible-round-trip",
            "reopen_gate": "validate-before-reopen",
            "ambiguous_external_result": "persist-operation-id-and-reconcile",
        },
    }


class UpgradeContractTests(unittest.TestCase):
    def test_schema_and_protocol_graph(self) -> None:
        schema = json.loads((ROOT / "schema/upgrade-contract.schema.json").read_text())
        document = contract()
        jsonschema.Draft202012Validator(schema).validate(document)
        validate_contract(document)
        phases = document["phases"]
        self.assertEqual([phase["order"] for phase in phases], list(range(1, 9)))
        self.assertEqual(
            [phase["id"] for phase in phases],
            [
                "discover",
                "preflight",
                "quiesce",
                "backup",
                "stage",
                "commit",
                "validate",
                "reopen",
            ],
        )
        self.assertTrue(
            next(phase for phase in phases if phase["id"] == "commit")["mutates_authority"]
        )
        commit = next(phase for phase in phases if phase["id"] == "commit")
        self.assertEqual(set(commit["requires"]), {"quiesce", "backup", "stage"})
        self.assertEqual(
            next(phase for phase in phases if phase["id"] == "reopen")["requires"], ["validate"]
        )

    def test_graph_rejects_duplicate_or_forward_dependencies(self) -> None:
        document = contract()
        phases = document["phases"]
        phases[4]["order"] = 4
        with self.assertRaises(AssertionError):
            self._assert_graph(phases)
        phases[4]["order"] = 5
        phases[4]["requires"] = ["validate"]
        with self.assertRaises(AssertionError):
            self._assert_graph(phases)

    def test_schema_requires_all_eight_phases(self) -> None:
        schema = json.loads((ROOT / "schema/upgrade-contract.schema.json").read_text())
        document = contract()
        document["phases"] = [phase for phase in document["phases"] if phase["id"] != "discover"]
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(document)

    def test_semantic_validator_rejects_noop_and_unbound_operation(self) -> None:
        document = contract()
        document["to"] = document["from"].copy()
        with self.assertRaises(ContractError):
            validate_contract(document)
        document = contract()
        document["to"]["version"] = document["from"]["version"]
        document["to"]["source_commit"] = "9" * 40
        with self.assertRaises(ContractError):
            validate_contract(document)
        document = contract()
        document["phases"][0]["operation"]["operation_id"] = "other-operation"
        with self.assertRaises(ContractError):
            validate_contract(document)

    def test_semantic_validator_rejects_phase_and_identity_mismatches(self) -> None:
        document = contract()
        document["from"]["tag_ref"] = "refs/tags/v9.9.9"
        with self.assertRaises(ContractError):
            validate_contract(document)

    def test_semantic_helper_branches_and_cli(self) -> None:
        document = contract()
        document["phases"][0]["id"] = "invalid"
        with self.assertRaises(ContractError):
            validate_phases(document)
        document = contract()
        document["phases"][0]["order"] = 2
        with self.assertRaises(ContractError):
            validate_phases(document)
        document = contract()
        document["phases"][0]["operation"]["operation_id"] = document["phases"][1]["operation"][
            "operation_id"
        ]
        with self.assertRaises(ContractError):
            validate_phases(document)
        document = contract()
        document["backend_contracts"] = [document["backend_contracts"][0]]
        with self.assertRaises(ContractError):
            validate_backends(document)
        document = contract()
        document["rollback"]["integrity_by_backend"]["git"] = "bad"
        with self.assertRaises(ContractError):
            validate_backends(document)
        self.assertEqual(validate_main([]), 2)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"
            path.write_text(json.dumps(contract()))
            self.assertEqual(validate_main([str(path)]), 0)
            path.write_text("{")
            self.assertEqual(validate_main([str(path)]), 1)
        document = contract()
        document["phases"][2]["mutates_authority"] = True
        with self.assertRaises(ContractError):
            validate_contract(document)
        document = contract()
        document["phases"][4]["requires"] = ["quiesce"]
        with self.assertRaises(ContractError):
            validate_contract(document)
        document = contract()
        document["rollback"]["backup_integrity"] = "hash-and-size"
        with self.assertRaises(ContractError):
            validate_contract(document)

    def test_self_consistency_and_backend_obligations(self) -> None:
        document = contract()
        phases = document["phases"]
        self.assertFalse(
            any(phase["mutates_authority"] and phase["id"] != "commit" for phase in phases)
        )
        commit = next(phase for phase in phases if phase["id"] == "commit")
        self.assertIn("stage", commit["requires"])
        self.assertEqual(
            {item["backend"] for item in document["backend_contracts"]},
            {"git", "sqlite"},
        )
        self.assertTrue(any("wal" in item for item in document["backend_contracts"]))

    @staticmethod
    def _assert_graph(phases: list[dict[str, Any]]) -> None:
        orders = [phase["order"] for phase in phases]
        assert orders == list(range(1, len(phases) + 1))
        by_id = {phase["id"]: phase["order"] for phase in phases}
        for phase in phases:
            for dependency in phase["requires"]:
                assert dependency in by_id
                assert by_id[dependency] < phase["order"]


if __name__ == "__main__":
    unittest.main()
