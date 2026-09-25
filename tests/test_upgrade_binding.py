# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import copy
import unittest
import uuid
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any
from unittest.mock import patch

from tools import upgrade_binding as binding_module
from tools.generate_upgrade_contract import generate
from tools.rollback_control_store import BarrierSessionState
from tools.upgrade_binding import UpgradeBindingError, UpgradeRuntimeBinding
from tools.upgrade_identity import (
    BarrierChildIdentity,
    BarrierSessionIdentity,
    canonical_barrier_digest,
    canonical_barrier_session_digest,
    canonical_envelope_digest,
)


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
        binding = UpgradeRuntimeBinding.bind(
            contract, _envelope(contract), session_identity_digest="a" * 64
        )
        self.assertEqual(contract["operation_id"], binding.contract_operation_id)
        self.assertEqual("barrier-7", binding.contract_barrier_id)
        self.assertEqual(
            set(binding.as_mapping()),
            {
                "schema_version",
                "contract_digest",
                "session_identity_digest",
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
                UpgradeRuntimeBinding.bind(
                    changed, _envelope(contract), session_identity_digest="a" * 64
                )

    def test_rejects_runtime_identity_drift_and_unknown_binding_fields(self) -> None:
        contract = _contract()
        runtime = _envelope(contract)
        for field, value in (("fencing_token", "foreign-fence"), ("target", "new")):
            changed = dict(runtime)
            changed[field] = value
            changed["barrier_identity_digest"] = canonical_barrier_digest(changed)
            changed["envelope_digest"] = canonical_envelope_digest(changed)
            with self.subTest(field=field), self.assertRaises(UpgradeBindingError):
                UpgradeRuntimeBinding.bind(contract, changed, session_identity_digest="a" * 64)
        foreign = _envelope(contract)
        foreign["project_id"] = str(uuid.uuid4())
        foreign["barrier_identity_digest"] = canonical_barrier_digest(foreign)
        foreign["envelope_digest"] = canonical_envelope_digest(foreign)
        self.assertNotEqual(
            UpgradeRuntimeBinding.bind(contract, runtime, session_identity_digest="a" * 64),
            UpgradeRuntimeBinding.bind(contract, foreign, session_identity_digest="a" * 64),
        )
        binding = UpgradeRuntimeBinding.bind(
            contract, runtime, session_identity_digest="a" * 64
        ).as_mapping()
        binding["unexpected"] = True
        with self.assertRaises(UpgradeBindingError):
            UpgradeRuntimeBinding.from_mapping(binding)

    def test_validates_observed_held_rollback_session(self) -> None:
        contract = _contract()
        runtime = _envelope(contract)
        session_record: dict[str, object] = {
            "schema_version": 1,
            "project_id": runtime["project_id"],
            "attempt_id": "attempt-7",
            "state_revision": runtime["state_revision"],
            "authority_revision_at_acquire": runtime["authority_revision"],
            "durable_barrier_id": runtime["durable_barrier_id"],
            "fencing_token": runtime["fencing_token"],
            "fencing_owner": runtime["fencing_owner"],
        }
        session_record["identity_digest"] = canonical_barrier_session_digest(session_record)
        identity = BarrierSessionIdentity.from_record(session_record)
        child = BarrierChildIdentity.bind(identity, str(runtime["operation_id"]), "rollback")
        session = BarrierSessionState(identity, "held", 2, rollback_child=child)
        binding = UpgradeRuntimeBinding.bind(
            contract,
            runtime,
            session_identity_digest=identity.identity_digest,
        )
        binding.validate_live_session(session)
        with self.assertRaisesRegex(UpgradeBindingError, "session identity digest"):
            foreign_record = {**session_record, "attempt_id": "attempt-other"}
            foreign_record["identity_digest"] = canonical_barrier_session_digest(foreign_record)
            foreign_identity = BarrierSessionIdentity.from_record(foreign_record)
            binding.validate_live_session(
                BarrierSessionState(
                    foreign_identity,
                    "held",
                    2,
                    rollback_child=BarrierChildIdentity.bind(
                        foreign_identity, str(runtime["operation_id"]), "rollback"
                    ),
                )
            )

    def test_rejects_malformed_binding_records(self) -> None:
        contract = _contract()
        binding = UpgradeRuntimeBinding.bind(
            contract, _envelope(contract), session_identity_digest="a" * 64
        ).as_mapping()
        mutations: list[dict[str, object]] = []
        mutations.append({key: value for key, value in binding.items() if key != "schema_version"})
        mutations.append({**binding, "schema_version": 2})
        mutations.append({**binding, "runtime_envelope": {}})
        mutations.extend(
            {
                **binding,
                field: "",
            }
            for field in (
                "contract_digest",
                "session_identity_digest",
                "contract_operation_id",
                "contract_backend",
                "contract_selector_ref",
                "contract_barrier_id",
                "contract_fencing_token",
                "contract_backup_operation_id",
            )
        )
        mutations.extend(
            [
                {**binding, "contract_digest": "x" * 64},
                {**binding, "session_identity_digest": "x" * 64},
                {**binding, "contract_expected_state_revision": 0},
            ]
        )
        for value in mutations:
            with self.subTest(value=value), self.assertRaises(UpgradeBindingError):
                UpgradeRuntimeBinding.from_mapping(value)

    def test_rejects_live_session_status_and_identity_drift(self) -> None:
        contract = _contract()
        runtime = _envelope(contract)
        record: dict[str, object] = {
            "schema_version": 1,
            "project_id": runtime["project_id"],
            "attempt_id": "attempt-7",
            "state_revision": runtime["state_revision"],
            "authority_revision_at_acquire": runtime["authority_revision"],
            "durable_barrier_id": runtime["durable_barrier_id"],
            "fencing_token": runtime["fencing_token"],
            "fencing_owner": runtime["fencing_owner"],
        }
        record["identity_digest"] = canonical_barrier_session_digest(record)
        identity = BarrierSessionIdentity.from_record(record)
        binding = UpgradeRuntimeBinding.bind(
            contract, runtime, session_identity_digest=identity.identity_digest
        )
        child = BarrierChildIdentity.bind(identity, str(runtime["operation_id"]), "rollback")
        cases = [
            BarrierSessionState(identity, "releasing", 2, rollback_child=child),
            BarrierSessionState(identity, "held", 2),
            BarrierSessionState(
                identity,
                "held",
                2,
                rollback_child=BarrierChildIdentity(
                    str(runtime["operation_id"]), "new", identity.identity_digest
                ),
            ),
        ]
        for case in cases:
            with self.assertRaises(UpgradeBindingError):
                binding.validate_live_session(case)
        with self.assertRaises(UpgradeBindingError):
            binding.validate_live_session(object())
        for field, value in (
            ("project_id", str(uuid.uuid4())),
            ("state_revision", 8),
            ("authority_revision_at_acquire", "other-authority"),
            ("durable_barrier_id", "other-barrier"),
            ("fencing_token", "other-fence"),
        ):
            changed = {**record, field: value}
            changed["identity_digest"] = canonical_barrier_session_digest(changed)
            changed_identity = BarrierSessionIdentity.from_record(changed)
            changed_identity = replace(changed_identity, identity_digest=identity.identity_digest)
            changed_child = BarrierChildIdentity.bind(
                changed_identity, str(runtime["operation_id"]), "rollback"
            )
            with self.subTest(field=field), self.assertRaises(UpgradeBindingError):
                binding.validate_live_session(
                    BarrierSessionState(changed_identity, "held", 2, rollback_child=changed_child)
                )

    def test_covers_strict_binding_defensive_boundaries(self) -> None:
        contract = _contract()
        with self.assertRaises(UpgradeBindingError):
            binding_module.canonical_contract_digest({"value": object()})
        with self.assertRaises(UpgradeBindingError):
            binding_module._contract_inputs({"rollback": {"operation": {"inputs": []}}})
        with self.assertRaises(UpgradeBindingError):
            binding_module._validated_envelope({})

        inputs = {
            "backend": "sqlite",
            "selector_ref": ".runtime/runtime-selector.json",
            "expected_state_revision": 7,
            "barrier_id": "barrier-7",
            "fencing_token": "fence-7",
            "backup_operation_id": "op:backup",
        }
        value: dict[str, Any] = {
            "operation_id": "op",
            "backend": "sqlite",
            "rollback": {"operation": {"operation_id": "op:rollback", "inputs": inputs}},
            "phases": [{"operation": {"inputs": dict(inputs)}}],
        }
        with patch.object(
            binding_module, "validate_runtime_contract", side_effect=lambda candidate: candidate
        ):
            for mutation in (
                {**inputs, "unexpected": True},
                {**inputs, "selector_ref": "other"},
            ):
                changed = copy.deepcopy(value)
                changed["rollback"]["operation"]["inputs"] = mutation
                with self.assertRaises(UpgradeBindingError):
                    binding_module._validate_contract_identity(changed)
            changed = copy.deepcopy(value)
            changed["phases"][0]["operation"]["inputs"]["barrier_id"] = "other"
            with self.assertRaises(UpgradeBindingError):
                binding_module._validate_contract_identity(changed)
            changed = copy.deepcopy(value)
            changed["backend"] = "git"
            with self.assertRaises(UpgradeBindingError):
                binding_module._validate_contract_identity(changed)
            changed = copy.deepcopy(value)
            changed["rollback"]["operation"]["inputs"]["backup_operation_id"] = "other"
            with self.assertRaises(UpgradeBindingError):
                binding_module._validate_contract_identity(changed)
            changed = copy.deepcopy(value)
            changed["rollback"]["operation"]["operation_id"] = "other"
            with self.assertRaises(UpgradeBindingError):
                binding_module._validate_contract_identity(changed)

        with self.assertRaises(UpgradeBindingError):
            UpgradeRuntimeBinding.bind(contract, _envelope(contract), session_identity_digest="bad")
        UpgradeRuntimeBinding.bind(contract, _envelope(contract), session_identity_digest="a" * 64)
        runtime = _envelope(contract)
        runtime["operation_id"] = "other"
        runtime["barrier_identity_digest"] = canonical_barrier_digest(runtime)
        runtime["envelope_digest"] = canonical_envelope_digest(runtime)
        with self.assertRaises(UpgradeBindingError):
            UpgradeRuntimeBinding.bind(contract, runtime, session_identity_digest="a" * 64)
        runtime = _envelope(contract)
        runtime["backend"] = "git"
        runtime["barrier_identity_digest"] = canonical_barrier_digest(runtime)
        runtime["envelope_digest"] = canonical_envelope_digest(runtime)
        with self.assertRaises(UpgradeBindingError):
            UpgradeRuntimeBinding.bind(contract, runtime, session_identity_digest="a" * 64)
