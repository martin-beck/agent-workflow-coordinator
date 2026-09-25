# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import copy
import unittest
import uuid
from pathlib import PurePosixPath
from typing import Any

from tools.generate_upgrade_contract import generate
from tools.upgrade_binding import UpgradeBindingError, UpgradeRuntimeBinding
from tools.upgrade_identity import canonical_barrier_digest, canonical_envelope_digest


def _contract() -> dict[str, Any]:
    def release(version: str, seed: str) -> dict[str, str]:
        return {
            "version": version,
            "source_commit": seed * 40,
            "tag_ref": f"refs/tags/{version}",
            "tag_object": chr(ord(seed) + 1) * 40,
            "signature_sha256": chr(ord(seed) + 2) * 64,
            "trust_policy_sha256": chr(ord(seed) + 3) * 64,
            "vendor_manifest_sha256": chr(ord(seed) + 4) * 64,
        }

    return generate(
        {
            "operation_id": "upgrade-001",
            "backend": "sqlite",
            "selector_ref": ".runtime/runtime-selector.json",
            "expected_state_revision": 7,
            "barrier_id": "barrier-7",
            "fencing_token": "fence-7",
            "from": release("v0.3.5", "a"),
            "to": release("v0.3.6", "b"),
        }
    )


def _envelope(contract: dict[str, Any]) -> dict[str, object]:
    root = PurePosixPath("/srv/runtime/artifacts")
    value: dict[str, object] = {
        "schema_version": 2,
        "backend": "sqlite",
        "project_id": str(uuid.uuid4()),
        "operation_id": contract["operation_id"],
        "state_revision": 7,
        "authority_revision": "authority-7",
        "fencing_token": "fence-7",
        "fencing_owner": "worker-7",
        "durable_barrier_id": "barrier-7",
        "artifact_root": str(root),
        "source": str(root / "source"),
        "destination": str(root / "destination"),
        "manifest": str(root / "manifest.json"),
        "selector_ref": ".runtime/runtime-selector.json",
        "target": "rollback",
    }
    value["barrier_identity_digest"] = canonical_barrier_digest(value)
    value["envelope_digest"] = canonical_envelope_digest(value)
    return value


class UpgradeBindingTests(unittest.TestCase):
    def test_binding_carries_and_validates_contract_and_runtime_identities(self) -> None:
        contract = _contract()
        binding = UpgradeRuntimeBinding.bind(contract, _envelope(contract))
        self.assertEqual(contract["operation_id"], binding.contract_operation_id)
        self.assertEqual("barrier-7", binding.contract_barrier_id)
        self.assertEqual(
            set(binding.as_mapping()),
            {
                "schema_version",
                "contract_digest",
                "contract_operation_id",
                "contract_backend",
                "contract_selector_ref",
                "contract_expected_state_revision",
                "contract_barrier_id",
                "contract_fencing_token",
                "contract_backup_operation_id",
                "runtime_envelope",
            },
        )
        restored = UpgradeRuntimeBinding.from_mapping(binding.as_mapping())
        self.assertEqual(binding, restored)

    def test_rejects_host_identity_drift(self) -> None:
        contract = _contract()
        for field, value in (
            ("operation_id", "other-operation"),
            ("backend", "git"),
            ("selector_ref", "other-selector.json"),
            ("expected_state_revision", 8),
            ("barrier_id", "other-barrier"),
            ("fencing_token", "other-fence"),
        ):
            changed = copy.deepcopy(contract)
            if field in {
                "operation_id",
                "backend",
                "selector_ref",
                "expected_state_revision",
                "barrier_id",
                "fencing_token",
            }:
                changed["rollback"]["operation"]["inputs"][field] = value
                for phase in changed["phases"]:
                    phase["operation"]["inputs"][field] = value
                if field == "operation_id":
                    changed["operation_id"] = value
            with self.subTest(field=field), self.assertRaises(UpgradeBindingError):
                UpgradeRuntimeBinding.bind(changed, _envelope(contract))

    def test_rejects_runtime_identity_drift_and_unknown_binding_fields(self) -> None:
        contract = _contract()
        runtime = _envelope(contract)
        for field, value in (("fencing_token", "foreign-fence"), ("target", "new")):
            changed = dict(runtime)
            changed[field] = value
            changed["barrier_identity_digest"] = canonical_barrier_digest(changed)
            changed["envelope_digest"] = canonical_envelope_digest(changed)
            with self.subTest(field=field), self.assertRaises(UpgradeBindingError):
                UpgradeRuntimeBinding.bind(contract, changed)
        foreign = _envelope(contract)
        foreign["project_id"] = str(uuid.uuid4())
        foreign["barrier_identity_digest"] = canonical_barrier_digest(foreign)
        foreign["envelope_digest"] = canonical_envelope_digest(foreign)
        self.assertNotEqual(
            UpgradeRuntimeBinding.bind(contract, runtime),
            UpgradeRuntimeBinding.bind(contract, foreign),
        )
        binding = UpgradeRuntimeBinding.bind(contract, runtime).as_mapping()
        binding["unexpected"] = True
        with self.assertRaises(UpgradeBindingError):
            UpgradeRuntimeBinding.from_mapping(binding)
