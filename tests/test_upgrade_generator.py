# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Hostile tests for deterministic release-contract generation."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
SPEC = importlib.util.spec_from_file_location(
    "upgrade_generator", ROOT / "tools/generate_upgrade_contract.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
ContractError = MODULE.ContractError
generate = MODULE.generate


def release(version: str, seed: str) -> dict[str, str]:
    return {
        "version": version,
        "source_commit": seed * 40,
        "tag_ref": f"refs/tags/{version}",
        "tag_object": (chr(ord(seed) + 1)) * 40,
        "signature_sha256": (chr(ord(seed) + 2)) * 64,
        "trust_policy_sha256": (chr(ord(seed) + 3)) * 64,
        "vendor_manifest_sha256": (chr(ord(seed) + 4)) * 64,
    }


def transition() -> dict[str, Any]:
    return {
        "operation_id": "upgrade:v0.3.5-to-v0.3.6:001",
        "from": release("v0.3.5", "a"),
        "to": release("v0.3.6", "b"),
    }


class UpgradeGeneratorTests(unittest.TestCase):
    def test_generation_is_deterministic_and_valid(self) -> None:
        first = generate(transition())
        second = generate(transition())
        self.assertEqual(first, second)
        self.assertEqual([phase["id"] for phase in first["phases"]], list(MODULE.PHASES))
        self.assertEqual(
            first["phases"][5]["operation"]["operation_id"], first["operation_id"] + ":commit"
        )

    def test_unknown_input_field_is_rejected_before_generation(self) -> None:
        document = transition()
        document["arbitrary_script"] = "rm -rf /"
        with self.assertRaises(ContractError):
            MODULE._validate_transition(document)
        with self.assertRaises(ContractError):
            MODULE._validate_transition([])
        document = transition()
        document["operation_id"] = 7
        with self.assertRaises(ContractError):
            MODULE._validate_transition(document)
        document = transition()
        document["from"] = []
        with self.assertRaises(ContractError):
            MODULE._validate_transition(document)

    def test_input_loader_and_cli_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            malformed = Path(directory) / "malformed.json"
            malformed.write_text("{", encoding="utf-8")
            with self.assertRaises(ContractError):
                MODULE._load_transition(malformed)
            with self.assertRaises(ContractError):
                MODULE._load_transition(Path(directory) / "missing.json")
            self.assertEqual(MODULE.main([str(malformed), str(Path(directory) / "out.json")]), 1)

    def test_floating_ref_and_noop_transition_are_rejected(self) -> None:
        document = transition()
        document["to"]["tag_ref"] = "refs/tags/latest"
        with self.assertRaises(ContractError):
            generate(document)
        document = transition()
        document["to"] = document["from"].copy()
        with self.assertRaises(ContractError):
            generate(document)

    def test_cli_writes_only_valid_json_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "transition.json"
            output = Path(directory) / "contract.json"
            source.write_text(json.dumps(transition()), encoding="utf-8")
            self.assertEqual(MODULE.main([str(source), str(output)]), 0)
            self.assertEqual(
                generate(json.loads(source.read_text())), json.loads(output.read_text())
            )


if __name__ == "__main__":
    unittest.main()
