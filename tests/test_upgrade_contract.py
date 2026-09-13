# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Tests for the correctness-first release-upgrade contract."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

import jsonschema

ROOT = Path(__file__).resolve().parents[1]


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
                "requires": [] if order == 1 else [phases[order - 2]],
                "on_failure": "restore-known-good",
                "operation": {
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
            "backup_integrity": "hash-and-size",
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
        self.assertEqual(
            next(phase for phase in phases if phase["id"] == "commit")["requires"], ["stage"]
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
