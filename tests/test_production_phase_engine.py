# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import unittest
from pathlib import Path

from tools.generate_upgrade_contract import generate
from tools.production_phase_engine import (
    ProductionPhaseBindingError,
    _phase_operations,
    build_production_phase_binding,
)


def _contract() -> dict[str, object]:
    return generate(
        {
            "operation_id": "phase-factory-test",
            "backend": "sqlite",
            "selector_ref": ".runtime/runtime-selector.json",
            "expected_state_revision": 1,
            "barrier_id": "barrier-test",
            "fencing_token": "fence-test",
            "from": {
                "version": "v0.1.0",
                "source_commit": "a" * 40,
                "tag_ref": "refs/tags/v0.1.0",
                "tag_object": "b" * 40,
                "trust_policy_sha256": "c" * 64,
                "vendor_manifest_sha256": "d" * 64,
            },
            "to": {
                "version": "v0.2.0",
                "source_commit": "e" * 40,
                "tag_ref": "refs/tags/v0.2.0",
                "tag_object": "f" * 40,
                "trust_policy_sha256": "0" * 64,
                "vendor_manifest_sha256": "1" * 64,
            },
        }
    )


class ProductionPhaseEngineTests(unittest.TestCase):
    def test_factory_extracts_all_exact_generated_phase_operations(self) -> None:
        contract = _contract()
        operations = _phase_operations(contract)

        self.assertEqual(
            set(operations),
            {"discover", "preflight", "quiesce", "backup", "stage", "commit", "validate", "reopen"},
        )
        self.assertEqual(
            operations["commit"]["operation_id"],
            "phase-factory-test:commit",
        )
        self.assertEqual(operations["commit"]["opcode"], "authority.atomic_replace")

    def test_factory_rejects_foreign_or_unissued_binding_before_engine_creation(self) -> None:
        with self.assertRaises(ProductionPhaseBindingError):
            build_production_phase_binding(_contract(), object(), Path("journal.json"))  # type: ignore[arg-type]

    def test_factory_rejects_tampered_phase_identity(self) -> None:
        contract = _contract()
        phases = contract["phases"]
        assert isinstance(phases, list)
        first = dict(phases[0])
        first["operation"] = dict(first["operation"], operation_id="foreign:discover")
        phases[0] = first

        with self.assertRaises(ProductionPhaseBindingError):
            _phase_operations(contract)


if __name__ == "__main__":
    unittest.main()
