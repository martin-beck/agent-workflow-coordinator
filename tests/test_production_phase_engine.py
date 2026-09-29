# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools.generate_upgrade_contract import generate
from tools.production_phase_engine import (
    BoundProductionBackendAdapter,
    ProductionPhaseBindingError,
    _phase_operations,
    build_production_phase_binding,
)
from tools.sqlite_authority_adapter import SQLiteAuthorityAdapter
from tools.upgrade_binding import LiveUpgradeBinding, canonical_contract_digest
from tools.upgrade_identity import canonical_barrier_digest, canonical_envelope_digest


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
    def _bound_backend(
        self, kind: str
    ) -> tuple[BoundProductionBackendAdapter, Mock, SimpleNamespace]:
        adapter = Mock()
        adapter.snapshot_bound.return_value = {"backend": kind, "mutates_authority": False}
        adapter.verify_rollback_context_bound.return_value = {
            "backend": kind,
            "rollback_context_verified": False,
        }
        adapter.execute.return_value = {"backend": kind, "mutates_authority": False}
        binding = SimpleNamespace(
            scope=object(),
            lease=object(),
            admission_recheck=object(),
            expected_branch="main" if kind == "git" else None,
            expected_head="a" * 40 if kind == "git" else None,
            adapter=adapter,
            is_admitted=Mock(return_value=True),
        )
        backend = object.__new__(BoundProductionBackendAdapter)
        backend._binding = binding
        backend._adapter = adapter
        backend.bound_rollback_kind = kind
        return backend, adapter, binding

    def test_bound_git_backend_rechecks_identity_for_evidence_and_execution(self) -> None:
        backend, adapter, binding = self._bound_backend("git")
        context = {"backend": "git"}

        self.assertEqual(backend.snapshot("discover", context)["backend"], "git")
        self.assertEqual(backend.verify_rollback_context(context)["backend"], "git")
        self.assertEqual(backend.execute("commit", context)["backend"], "git")
        backend.verify_rollback_context_bound(
            context,
            binding.scope,
            lease=binding.lease,
            admission_recheck=binding.admission_recheck,
            expected_branch=binding.expected_branch,
            expected_head=binding.expected_head,
        )
        self.assertEqual(adapter.snapshot_bound.call_count, 1)
        self.assertEqual(adapter.verify_rollback_context_bound.call_count, 2)
        self.assertEqual(adapter.execute.call_count, 1)

    def test_bound_sqlite_backend_uses_sqlite_signature_and_rejects_identity_change(self) -> None:
        backend, adapter, binding = self._bound_backend("sqlite")
        context = {"backend": "sqlite"}

        backend.snapshot("validate", context)
        backend.verify_rollback_context(context)
        backend.verify_rollback_context_bound(
            context,
            binding.scope,
            lease=binding.lease,
            admission_recheck=binding.admission_recheck,
        )
        with self.assertRaises(ProductionPhaseBindingError):
            backend.verify_rollback_context_bound(
                context,
                object(),
                lease=binding.lease,
                admission_recheck=binding.admission_recheck,
            )
        self.assertEqual(adapter.snapshot_bound.call_count, 1)

    def test_bound_backend_fails_closed_when_live_binding_is_not_admitted(self) -> None:
        backend, _adapter, binding = self._bound_backend("sqlite")
        binding.is_admitted.return_value = False

        with self.assertRaises(ProductionPhaseBindingError):
            backend.snapshot("discover", {"backend": "sqlite"})

    def test_bound_backend_constructor_and_result_shapes_are_strict(self) -> None:
        binding = object.__new__(LiveUpgradeBinding)
        adapter = object.__new__(SQLiteAuthorityAdapter)
        object.__setattr__(binding, "adapter", adapter)
        with patch.object(LiveUpgradeBinding, "is_admitted", return_value=True):
            backend = BoundProductionBackendAdapter(binding)
        self.assertIsNone(backend.operation_lock)
        backend._binding = SimpleNamespace(
            is_admitted=Mock(return_value=True),
            scope=object(),
            lease=object(),
            admission_recheck=object(),
        )
        backend._adapter = Mock()
        backend._adapter.snapshot_bound.return_value = []
        backend._adapter.verify_rollback_context_bound.return_value = []
        backend._adapter.execute.return_value = []
        backend.bound_rollback_kind = "sqlite"
        with self.assertRaises(ProductionPhaseBindingError):
            backend.snapshot("discover", {})
        with self.assertRaises(ProductionPhaseBindingError):
            backend.verify_rollback_context({})
        with self.assertRaises(ProductionPhaseBindingError):
            backend.execute("discover", {})

    def test_bound_backend_rejects_scope_lease_recheck_and_git_identity_changes(self) -> None:
        backend, _adapter, binding = self._bound_backend("git")
        with self.assertRaises(ProductionPhaseBindingError):
            backend.verify_rollback_context_bound(
                {}, object(), lease=binding.lease, admission_recheck=binding.admission_recheck
            )
        with self.assertRaises(ProductionPhaseBindingError):
            backend.verify_rollback_context_bound(
                {}, binding.scope, lease=object(), admission_recheck=binding.admission_recheck
            )
        with self.assertRaises(ProductionPhaseBindingError):
            backend.verify_rollback_context_bound(
                {},
                binding.scope,
                lease=binding.lease,
                admission_recheck=binding.admission_recheck,
                expected_branch="foreign",
                expected_head=binding.expected_head,
            )

    def test_phase_operation_validation_rejects_incomplete_shapes(self) -> None:
        contract = _contract()
        for changed in (
            {"phases": []},
            {"phases": contract["phases"], "operation_id": None},
        ):
            invalid = dict(contract)
            invalid.update(changed)
            with self.assertRaises(ProductionPhaseBindingError):
                _phase_operations(invalid)
        phases = list(contract["phases"])
        phases[0] = {"id": "foreign"}
        invalid = dict(contract, phases=phases)
        with self.assertRaises(ProductionPhaseBindingError):
            _phase_operations(invalid)
        phases = list(contract["phases"])
        phases[0] = dict(phases[0], operation=None)
        with self.assertRaises(ProductionPhaseBindingError):
            _phase_operations(dict(contract, phases=phases))

    def test_factory_rejects_invalid_contract_and_binding_digest(self) -> None:
        with self.assertRaises(ProductionPhaseBindingError):
            build_production_phase_binding({}, object(), Path("journal.json"))  # type: ignore[arg-type]
        contract = _contract()
        binding = object.__new__(LiveUpgradeBinding)
        runtime = SimpleNamespace(
            contract_digest="0" * 64,
            contract_backend="sqlite",
            runtime_envelope={},
        )
        object.__setattr__(binding, "runtime", runtime)
        with (
            patch.object(LiveUpgradeBinding, "is_admitted", return_value=True),
            self.assertRaises(ProductionPhaseBindingError),
        ):
            build_production_phase_binding(contract, binding, Path("journal.json"))

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

    def test_factory_constructs_engine_from_exact_admitted_runtime_envelope(self) -> None:
        contract = _contract()
        root = "/artifacts"
        envelope: dict[str, object] = {
            "schema_version": 2,
            "backend": "sqlite",
            "project_id": str(uuid.uuid4()),
            "operation_id": contract["operation_id"],
            "state_revision": 1,
            "authority_revision": "authority-test",
            "fencing_token": "fence-test",
            "fencing_owner": "owner-test",
            "durable_barrier_id": "barrier-test",
            "artifact_root": root,
            "source": f"{root}/source",
            "destination": f"{root}/destination",
            "manifest": f"{root}/manifest.json",
            "selector_ref": ".runtime/runtime-selector.json",
            "target": "rollback",
        }
        envelope["barrier_identity_digest"] = canonical_barrier_digest(envelope)
        envelope["envelope_digest"] = canonical_envelope_digest(envelope)
        runtime = SimpleNamespace(
            contract_digest=canonical_contract_digest(contract),
            contract_backend="sqlite",
            runtime_envelope=envelope,
        )
        binding = object.__new__(LiveUpgradeBinding)
        for field, value in {
            "runtime": runtime,
            "session": object(),
            "scope": object(),
            "lease": object(),
            "admission_recheck": object(),
            "adapter": object.__new__(SQLiteAuthorityAdapter),
            "_token": object(),
            "expected_branch": None,
            "expected_head": None,
            "expected_git_repository": None,
        }.items():
            object.__setattr__(binding, field, value)

        with (
            patch.object(LiveUpgradeBinding, "is_admitted", return_value=True),
            patch.object(LiveUpgradeBinding, "matches_contract", return_value=True),
        ):
            result = build_production_phase_binding(contract, binding, Path("journal.json"))

        self.assertEqual(result.engine.check()["operation_id"], contract["operation_id"])
        self.assertEqual(result.operations["reopen"]["operation_id"], "phase-factory-test:reopen")


if __name__ == "__main__":
    unittest.main()
