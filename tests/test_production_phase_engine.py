# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from tools.admission_lease import AdmissionLease, AdmissionRecheck
from tools.authority_mutation import DurableBoundBackendMutation
from tools.authority_neutral_commit import CommitAdmissionBundle
from tools.generate_upgrade_contract import generate
from tools.handoffctl import locked
from tools.lock_domain_scope import LockDomainScope
from tools.mutation_fence import MutationFence, provision, provision_control_binding
from tools.production_phase_engine import (
    BoundProductionBackendAdapter,
    ForwardCommitCapabilityInputs,
    ForwardPhaseCapabilityInputs,
    ProductionPhaseBindingError,
    _phase_operations,
    _validate_forward_commit_evidence,
    _validate_forward_context,
    _validate_forward_validation_inputs,
    bind_forward_phase_capabilities,
    build_production_phase_binding,
)
from tools.rollback_control_store import (
    SQLiteBarrierSessionStore,
    SQLiteRollbackControlStore,
)
from tools.runtime_bootstrap import DispatchAdmission
from tools.sqlite_authority_adapter import (
    SQLiteAuthorityAdapter,
    SQLiteAuthorityError,
    SQLiteLifecycleExecutor,
)
from tools.upgrade_admission import QUIESCENCE_PREDICATES
from tools.upgrade_binding import (
    LiveUpgradeBinding,
    UpgradeRuntimeBinding,
    canonical_contract_digest,
)
from tools.upgrade_identity import (
    BarrierChildIdentity,
    BarrierSessionIdentity,
    canonical_barrier_digest,
    canonical_barrier_session_digest,
    canonical_envelope_digest,
)


def _contract() -> dict[str, object]:
    return cast(
        dict[str, object],
        generate(
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
        ),
    )


class ProductionPhaseEngineTests(unittest.TestCase):
    def _factory_forward_fixture(
        self,
    ) -> tuple[dict[str, object], LiveUpgradeBinding, ForwardPhaseCapabilityInputs]:
        contract = _contract()
        envelope: dict[str, object] = {
            "schema_version": 2,
            "backend": "sqlite",
            "project_id": str(uuid.uuid4()),
            "operation_id": contract["operation_id"],
            "state_revision": 1,
            "authority_revision": "authority",
            "fencing_token": "fence-test",
            "fencing_owner": "owner",
            "durable_barrier_id": "barrier-test",
            "artifact_root": "/artifacts",
            "source": "/artifacts/source",
            "destination": "/artifacts/destination",
            "manifest": "/artifacts/manifest.json",
            "selector_ref": ".runtime/runtime-selector.json",
            "target": "rollback",
        }
        envelope["barrier_identity_digest"] = canonical_barrier_digest(envelope)
        envelope["envelope_digest"] = canonical_envelope_digest(envelope)
        forward = dict(envelope, target="new")
        forward["barrier_identity_digest"] = canonical_barrier_digest(forward)
        forward["envelope_digest"] = canonical_envelope_digest(forward)
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
        admission = object.__new__(DispatchAdmission)
        object.__setattr__(
            admission, "identity", SimpleNamespace(release="v0.2.0", digest="a" * 64)
        )
        commit = CommitAdmissionBundle(
            backend="sqlite",
            target="new",
            operation_id="phase-factory-test:commit",
            fencing_token="fence-test",  # noqa: S106
            state_revision=1,
            barrier_id="barrier-test",
            artifact_identity="artifact",
            manifest_identity="manifest",
            selector_identity="selector",
            runtime_identity="runtime",
        )
        snapshot = dict(forward)
        snapshot.update(dict.fromkeys(QUIESCENCE_PREDICATES, True))
        snapshot["barrier_status"] = "held"
        evidence = {"admitted_snapshot": snapshot, "current_snapshot": snapshot}
        inputs = ForwardPhaseCapabilityInputs(
            forward,
            dict(forward, binding={}),
            dict(
                forward, runtime_root="/artifacts/destination", manifest_digest="a" * 64, binding={}
            ),
            admission,
            dict(forward, binding={"release": "v0.2.0", "manifest_digest": "a" * 64}),
            ForwardCommitCapabilityInputs(
                commit, 1, lambda: dict(forward), "argument", evidence, object()
            ),
        )
        return contract, binding, inputs

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
        backend: Any = object.__new__(BoundProductionBackendAdapter)
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

    def test_generated_sqlite_dispatch_rechecks_admission_before_effect(self) -> None:
        backend, _adapter, binding = self._bound_backend("sqlite")
        executor = Mock()
        backend._sqlite_lifecycle_executor = executor
        binding.is_admitted.return_value = False

        with self.assertRaises(ProductionPhaseBindingError):
            backend.execute_generated_operation({}, Path("backup.sqlite"), {})
        executor.execute_generated_operation.assert_not_called()

    def test_constructor_binds_durable_sqlite_lifecycle_executor(self) -> None:
        from test_upgrade_binding import _contract as binding_contract
        from test_upgrade_binding import _envelope as binding_envelope

        runtime_contract = binding_contract()
        runtime_envelope = binding_envelope(runtime_contract)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = root / "authority.sqlite"
            with closing(sqlite3.connect(authority)) as connection, connection:
                connection.execute("CREATE TABLE state (id INTEGER PRIMARY KEY, value TEXT)")
                connection.execute("INSERT INTO state VALUES (1, 'old')")
            authority.chmod(0o600)
            project = str(runtime_envelope["project_id"])
            control = SQLiteRollbackControlStore(root / "control.sqlite", project, authority)
            session = SQLiteBarrierSessionStore(
                control, lambda: str(runtime_envelope["authority_revision"])
            )
            identity_record: dict[str, object] = {
                "schema_version": 1,
                "project_id": project,
                "attempt_id": "attempt-production",
                "state_revision": runtime_envelope["state_revision"],
                "authority_revision_at_acquire": runtime_envelope["authority_revision"],
                "durable_barrier_id": runtime_envelope["durable_barrier_id"],
                "fencing_token": runtime_envelope["fencing_token"],
                "fencing_owner": runtime_envelope["fencing_owner"],
                "identity_digest": "0" * 64,
            }
            identity_record["identity_digest"] = canonical_barrier_session_digest(
                identity_record
            )
            identity = BarrierSessionIdentity.from_record(identity_record)
            session.create(identity)
            session.bind_child(
                1, BarrierChildIdentity.bind(identity, "upgrade-001:forward", "new")
            )
            session.bind_child(
                2, BarrierChildIdentity.bind(identity, "upgrade-001", "rollback")
            )
            marker = root / "authority-marker.json"
            lifecycle = root / "authority-lifecycle.json"
            authority_lock = root / "authority.lock"
            control_binding = root / "control-binding.json"
            provision(authority, marker, lifecycle, authority_lock, project)
            provision_control_binding(
                control.path, control_binding, control.control_lock_path, project
            )
            fence = MutationFence(
                authority,
                marker,
                lifecycle,
                authority_lock,
                control.path,
                control_binding,
                control.control_lock_path,
            )
            lease = AdmissionLease(
                project_id=project,
                authority_revision=str(runtime_envelope["authority_revision"]),
                fencing_token=str(runtime_envelope["fencing_token"]),
                fencing_owner=str(runtime_envelope["fencing_owner"]),
                durable_barrier_id=str(runtime_envelope["durable_barrier_id"]),
                revision=cast(int, runtime_envelope["state_revision"]),
            )
            recheck = AdmissionRecheck(
                lease=lease,
                project_id=project,
                authority_revision=str(runtime_envelope["authority_revision"]),
                fencing_token=str(runtime_envelope["fencing_token"]),
                fencing_owner=str(runtime_envelope["fencing_owner"]),
                durable_barrier_id=str(runtime_envelope["durable_barrier_id"]),
                revision=cast(int, runtime_envelope["state_revision"]),
            )
            scope = LockDomainScope.bind(session, fence, lease, recheck, locked)
            runtime = UpgradeRuntimeBinding.bind(
                runtime_contract, runtime_envelope,
                session_identity_digest=identity.identity_digest,
            )
            adapter = SQLiteAuthorityAdapter(authority)
            live = LiveUpgradeBinding.bind(
                runtime, session.snapshot(), scope, lease, recheck, adapter
            )
            backend = BoundProductionBackendAdapter(live, journal=root / "journal.json")
            self.assertIsInstance(backend._sqlite_lifecycle_executor, SQLiteLifecycleExecutor)
            with self.assertRaises(SQLiteAuthorityError):
                backend.execute_generated_operation({}, root / "backup.sqlite", {})

    def test_bound_backend_constructor_and_result_shapes_are_strict(self) -> None:
        binding = object.__new__(LiveUpgradeBinding)
        adapter = object.__new__(SQLiteAuthorityAdapter)
        object.__setattr__(binding, "adapter", adapter)
        with patch.object(LiveUpgradeBinding, "is_admitted", return_value=True):
            backend: Any = BoundProductionBackendAdapter(binding)
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
        changed_cases: tuple[dict[str, object], ...] = (
            {"phases": []},
            {"phases": contract["phases"], "operation_id": None},
        )
        for changed in changed_cases:
            invalid = dict(contract)
            invalid.update(changed)
            with self.assertRaises(ProductionPhaseBindingError):
                _phase_operations(invalid)
        phases = list(cast(list[dict[str, object]], contract["phases"]))
        phases[0] = {"id": "foreign"}
        invalid = dict(contract, phases=phases)
        with self.assertRaises(ProductionPhaseBindingError):
            _phase_operations(invalid)
        phases = list(cast(list[dict[str, object]], contract["phases"]))
        phases[0] = dict(phases[0], operation=None)
        with self.assertRaises(ProductionPhaseBindingError):
            _phase_operations(dict(contract, phases=phases))

    def test_factory_rejects_invalid_contract_and_binding_digest(self) -> None:
        with self.assertRaises(ProductionPhaseBindingError):
            build_production_phase_binding({}, cast(Any, object()), Path("journal.json"))
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
            build_production_phase_binding(_contract(), cast(Any, object()), Path("journal.json"))

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

    def test_factory_binds_forward_readonly_capabilities_to_target_neutral_session(self) -> None:
        contract = _contract()
        root = "/artifacts"
        rollback_envelope: dict[str, object] = {
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
        rollback_envelope["barrier_identity_digest"] = canonical_barrier_digest(rollback_envelope)
        rollback_envelope["envelope_digest"] = canonical_envelope_digest(rollback_envelope)
        forward_envelope = dict(rollback_envelope, target="new")
        forward_envelope["barrier_identity_digest"] = canonical_barrier_digest(forward_envelope)
        forward_envelope["envelope_digest"] = canonical_envelope_digest(forward_envelope)
        runtime = SimpleNamespace(
            contract_digest=canonical_contract_digest(contract),
            contract_backend="sqlite",
            runtime_envelope=rollback_envelope,
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
        admission_identity = SimpleNamespace(release="v0.2.0", digest="a" * 64)
        backup_context = dict(forward_envelope, binding={})
        stage_context = dict(
            forward_envelope,
            runtime_root=f"{root}/destination",
            manifest_digest="a" * 64,
            binding={"release": "v0.2.0", "manifest_digest": "a" * 64},
        )
        validation_context = dict(
            forward_envelope,
            binding={"release": "v0.2.0", "manifest_digest": "a" * 64},
        )
        admission = object.__new__(DispatchAdmission)
        object.__setattr__(admission, "identity", admission_identity)
        commit_admission = CommitAdmissionBundle(
            backend="sqlite",
            target="new",
            operation_id="phase-factory-test:commit",
            fencing_token="fence-test",  # noqa: S106
            state_revision=1,
            barrier_id="barrier-test",
            artifact_identity="artifact",
            manifest_identity="manifest",
            selector_identity="selector",
            runtime_identity="runtime",
        )
        commit_snapshot = dict(forward_envelope)
        commit_snapshot.update(dict.fromkeys(QUIESCENCE_PREDICATES, True))
        commit_snapshot["barrier_status"] = "held"
        commit_evidence = {
            "quiesced": True,
            "backup_verified": True,
            "selector_verified": True,
            "selector_commit_atomic": True,
            "fencing_verified": True,
            "selector_before_verified": True,
            "selector_after_verified": True,
            "admitted_snapshot": commit_snapshot,
            "current_snapshot": commit_snapshot,
        }

        with (
            patch.object(LiveUpgradeBinding, "is_admitted", return_value=True),
            patch.object(LiveUpgradeBinding, "matches_contract", return_value=True),
            patch(
                "tools.production_phase_engine.bind_durable_commit_capability",
                return_value=cast(Any, object.__new__(DurableBoundBackendMutation)),
            ),
        ):
            result = build_production_phase_binding(
                contract,
                binding,
                Path("journal.json"),
                forward_inputs=ForwardPhaseCapabilityInputs(
                    forward_envelope,
                    backup_context,
                    stage_context,
                    admission,
                    validation_context,
                    ForwardCommitCapabilityInputs(
                        commit_admission,
                        1,
                        lambda: dict(forward_envelope),
                        "commit-argument",
                        commit_evidence,
                        object(),
                    ),
                ),
            )

        self.assertIsNotNone(result.forward_capabilities)
        self.assertEqual(result.engine.context.target, "new")
        self.assertIsNotNone(result.engine._backup_phase_adapter)
        self.assertIsNotNone(result.engine._stage_phase_adapter)
        self.assertIsNotNone(result.engine._validation_phase_adapter)
        self.assertIsNotNone(result.commit_capability)
        self.assertIsNotNone(result.engine._commit_phase_adapter)

    def test_forward_capability_binding_rejects_rollback_context_reuse(self) -> None:
        contract = _contract()
        binding = object.__new__(LiveUpgradeBinding)
        runtime = SimpleNamespace(
            contract_digest=canonical_contract_digest(contract),
            contract_backend="sqlite",
            runtime_envelope={"target": "rollback"},
        )
        object.__setattr__(binding, "runtime", runtime)
        with (
            patch.object(LiveUpgradeBinding, "is_admitted", return_value=True),
            self.assertRaises(ProductionPhaseBindingError),
        ):
            build_production_phase_binding(
                contract,
                binding,
                Path("journal.json"),
                forward_inputs=ForwardPhaseCapabilityInputs(
                    {"target": "rollback"}, {}, {}, object.__new__(DispatchAdmission), {}
                ),
            )

    def test_factory_consumes_real_sqlite_commit_capability(self) -> None:
        contract = _contract()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "authority.sqlite"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    "CREATE TABLE state (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute("INSERT INTO state VALUES (1, 'old')")
            database.chmod(0o600)
            with closing(sqlite3.connect(database)) as keepalive:
                keepalive.execute("PRAGMA wal_autocheckpoint=0")
                keepalive.commit()
            project = str(uuid.uuid4())
            rollback_envelope: dict[str, object] = {
                "schema_version": 2,
                "backend": "sqlite",
                "project_id": project,
                "operation_id": contract["operation_id"],
                "state_revision": 1,
                "authority_revision": "authority-test",
                "fencing_token": "fence-test",
                "fencing_owner": "owner-test",
                "durable_barrier_id": "barrier-test",
                "artifact_root": str(root / "artifacts"),
                "source": str(root / "source"),
                "destination": str(root / "artifacts" / "destination"),
                "manifest": str(root / "artifacts" / "manifest.json"),
                "selector_ref": ".runtime/runtime-selector.json",
                "target": "rollback",
            }
            Path(cast(str, rollback_envelope["artifact_root"])).mkdir()
            rollback_envelope["barrier_identity_digest"] = canonical_barrier_digest(
                rollback_envelope
            )
            rollback_envelope["envelope_digest"] = canonical_envelope_digest(rollback_envelope)
            forward_envelope = dict(rollback_envelope, target="new")
            forward_envelope["barrier_identity_digest"] = canonical_barrier_digest(forward_envelope)
            forward_envelope["envelope_digest"] = canonical_envelope_digest(forward_envelope)
            runtime = SimpleNamespace(
                contract_digest=canonical_contract_digest(contract),
                contract_backend="sqlite",
                runtime_envelope=rollback_envelope,
            )
            binding = object.__new__(LiveUpgradeBinding)
            for field, value in {
                "runtime": runtime,
                "session": object(),
                "scope": object(),
                "lease": object(),
                "admission_recheck": object(),
                "adapter": SQLiteAuthorityAdapter(database),
                "_token": object(),
                "expected_branch": None,
                "expected_head": None,
                "expected_git_repository": None,
            }.items():
                object.__setattr__(binding, field, value)
            validation_admission = object.__new__(DispatchAdmission)
            object.__setattr__(
                validation_admission,
                "identity",
                SimpleNamespace(release="v0.2.0", digest="a" * 64),
            )
            commit_admission = CommitAdmissionBundle(
                backend="sqlite",
                target="new",
                operation_id="phase-factory-test:commit",
                fencing_token="fence-test",  # noqa: S106
                state_revision=1,
                barrier_id="barrier-test",
                artifact_identity="artifact",
                manifest_identity="manifest",
                selector_identity="selector",
                runtime_identity="runtime",
            )
            commit_snapshot = dict(forward_envelope)
            commit_snapshot.update(dict.fromkeys(QUIESCENCE_PREDICATES, True))
            commit_snapshot["barrier_status"] = "held"
            commit_evidence = {
                "quiesced": True,
                "backup_verified": True,
                "selector_verified": True,
                "selector_commit_atomic": True,
                "fencing_verified": True,
                "selector_before_verified": True,
                "selector_after_verified": True,
                "admitted_snapshot": commit_snapshot,
                "current_snapshot": commit_snapshot,
            }

            class Journal:
                def prepare_authority_effect(self, *_args: object, **_kwargs: object) -> str:
                    return "factory-effect"

                def finish_authority_effect(
                    self, _intent: object, outcome: str, _receipt: object = None
                ) -> None:
                    self.outcome = outcome

            effect_journal = Journal()

            def reread() -> dict[str, object]:
                return {
                    "backend": "sqlite",
                    "target": "new",
                    "operation_id": commit_admission.operation_id,
                    "state_revision": 1,
                    "barrier_id": "barrier-test",
                    "fencing_token": "fence-test",
                    "artifact_identity": "artifact",
                    "manifest_identity": "manifest",
                    "selector_identity": "selector",
                    "runtime_identity": "runtime",
                }

            def update(connection: sqlite3.Connection) -> None:
                connection.execute("UPDATE state SET value='new' WHERE id=1")

            phase_inputs = ForwardPhaseCapabilityInputs(
                forward_envelope,
                dict(forward_envelope, binding={}),
                dict(
                    forward_envelope,
                    runtime_root=str(root / "artifacts" / "destination"),
                    manifest_digest="a" * 64,
                    binding={},
                ),
                validation_admission,
                dict(
                    forward_envelope,
                    binding={"release": "v0.2.0", "manifest_digest": "a" * 64},
                ),
                ForwardCommitCapabilityInputs(
                    commit_admission,
                    1,
                    reread,
                    update,
                    commit_evidence,
                    effect_journal,
                ),
            )
            with (
                patch.object(LiveUpgradeBinding, "is_admitted", return_value=True),
                patch.object(LiveUpgradeBinding, "matches_contract", return_value=True),
            ):
                result = build_production_phase_binding(
                    contract,
                    binding,
                    root / "engine-journal.json",
                    forward_inputs=phase_inputs,
                )
            assert result.engine._commit_phase_adapter is not None
            commit_result = result.engine._commit_phase_adapter.execute(
                "commit",
                {
                    "backend": "sqlite",
                    "target": "new",
                    "operation_id": "phase-factory-test:commit",
                    "state_revision": 1,
                    "durable_barrier_id": "barrier-test",
                    "fencing_token": "fence-test",
                },
            )
            self.assertTrue(commit_result["authority_effect_verified"])
            self.assertEqual("committed", effect_journal.outcome)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(("new",), connection.execute("SELECT value FROM state").fetchone())

    def test_forward_validation_and_commit_rejection_shapes_are_exhaustive(self) -> None:
        contract = _contract()
        rollback: dict[str, object] = {
            "schema_version": 2,
            "backend": "sqlite",
            "project_id": "project",
            "operation_id": contract["operation_id"],
            "state_revision": 1,
            "authority_revision": "authority",
            "fencing_token": "fence-test",
            "fencing_owner": "owner",
            "durable_barrier_id": "barrier-test",
            "artifact_root": "/artifacts",
            "source": "/artifacts/source",
            "destination": "/artifacts/destination",
            "manifest": "/artifacts/manifest.json",
            "selector_ref": ".runtime/runtime-selector.json",
            "target": "rollback",
        }
        rollback["barrier_identity_digest"] = canonical_barrier_digest(rollback)
        rollback["envelope_digest"] = canonical_envelope_digest(rollback)
        forward = dict(rollback, target="new")
        forward["barrier_identity_digest"] = canonical_barrier_digest(forward)
        forward["envelope_digest"] = canonical_envelope_digest(forward)
        binding = SimpleNamespace(runtime=SimpleNamespace(runtime_envelope=rollback))

        for invalid in (None, dict(forward, target="rollback"), dict(forward, extra=True)):
            with self.assertRaises(ProductionPhaseBindingError):
                _validate_forward_context(cast(Any, binding), cast(Any, invalid))
        changed = dict(forward, project_id="foreign")
        with self.assertRaises(ProductionPhaseBindingError):
            _validate_forward_context(cast(Any, binding), changed)
        with self.assertRaises(ProductionPhaseBindingError):
            _validate_forward_context(
                cast(Any, binding), dict(forward, barrier_identity_digest="bad")
            )
        with self.assertRaises(ProductionPhaseBindingError):
            _validate_forward_context(cast(Any, binding), dict(forward, envelope_digest="bad"))
        with self.assertRaises(ProductionPhaseBindingError):
            _validate_forward_context(
                cast(Any, SimpleNamespace(runtime=SimpleNamespace(runtime_envelope={}))), forward
            )

        admission = SimpleNamespace(identity=SimpleNamespace(release="v0.2.0", digest="a" * 64))
        with self.assertRaises(ProductionPhaseBindingError):
            _validate_forward_validation_inputs(forward, cast(Any, admission), cast(Any, None))
        with self.assertRaises(ProductionPhaseBindingError):
            _validate_forward_validation_inputs(
                forward, cast(Any, admission), dict(forward, backend="git")
            )
        with self.assertRaises(ProductionPhaseBindingError):
            _validate_forward_validation_inputs(
                forward, cast(Any, admission), dict(forward, binding={})
            )
        valid_validation = dict(
            forward,
            binding={"release": "v0.2.0", "manifest_digest": "a" * 64},
        )
        _validate_forward_validation_inputs(forward, cast(Any, admission), valid_validation)

        commit = CommitAdmissionBundle(
            backend="sqlite",
            target="new",
            operation_id="phase-factory-test:commit",
            fencing_token="fence-test",  # noqa: S106
            state_revision=1,
            barrier_id="barrier-test",
            artifact_identity="artifact",
            manifest_identity="manifest",
            selector_identity="selector",
            runtime_identity="runtime",
        )
        for invalid in cast(tuple[Any, ...], (None, {"admitted_snapshot": {}})):
            with self.assertRaises(ProductionPhaseBindingError):
                _validate_forward_commit_evidence(forward, commit, cast(Any, invalid))
        expected_snapshot = dict(forward)
        with self.assertRaises(ProductionPhaseBindingError):
            _validate_forward_commit_evidence(
                forward,
                commit,
                {"admitted_snapshot": expected_snapshot, "current_snapshot": {"backend": "git"}},
            )
        _validate_forward_commit_evidence(
            forward,
            commit,
            {"admitted_snapshot": expected_snapshot, "current_snapshot": expected_snapshot},
        )

    def test_forward_capability_factory_rejects_missing_operations_and_commit_wiring(self) -> None:
        contract = _contract()
        operations = _phase_operations(contract)
        binding = object.__new__(LiveUpgradeBinding)
        runtime = SimpleNamespace(
            contract_digest=canonical_contract_digest(contract), runtime_envelope={}
        )
        object.__setattr__(binding, "runtime", runtime)
        with (
            patch.object(LiveUpgradeBinding, "is_admitted", return_value=False),
            self.assertRaises(ProductionPhaseBindingError),
        ):
            bind_forward_phase_capabilities(binding, operations, cast(Any, object()))
        valid_binding = object.__new__(LiveUpgradeBinding)
        object.__setattr__(
            valid_binding, "runtime", SimpleNamespace(runtime_envelope={"target": "new"})
        )
        inputs = ForwardPhaseCapabilityInputs({}, {}, {}, cast(Any, object()), {})
        with (
            patch.object(LiveUpgradeBinding, "is_admitted", return_value=True),
            self.assertRaises(ProductionPhaseBindingError),
        ):
            bind_forward_phase_capabilities(valid_binding, {}, inputs)

        _ = operations
        _ = inputs

    def test_factory_commit_and_engine_rejection_paths_are_bound(self) -> None:
        contract, binding, inputs = self._factory_forward_fixture()
        cases = (
            dict(
                _phase_operations(contract),
                commit=dict(_phase_operations(contract)["commit"], opcode="unsupported"),
            ),
            dict(
                _phase_operations(contract),
                commit=dict(_phase_operations(contract)["commit"], operation_id="foreign"),
            ),
            dict(
                _phase_operations(contract),
                commit=dict(_phase_operations(contract)["commit"], inputs=None),
            ),
        )
        with (
            patch.object(LiveUpgradeBinding, "is_admitted", return_value=True),
            patch.object(LiveUpgradeBinding, "matches_contract", return_value=True),
        ):
            for operations in cases:
                with (
                    patch(
                        "tools.production_phase_engine._phase_operations", return_value=operations
                    ),
                    self.assertRaises(ProductionPhaseBindingError),
                ):
                    build_production_phase_binding(
                        contract, binding, Path("journal.json"), forward_inputs=inputs
                    )
            with (
                patch(
                    "tools.production_phase_engine.bind_durable_commit_capability",
                    side_effect=TypeError("bad"),
                ),
                self.assertRaises(ProductionPhaseBindingError),
            ):
                build_production_phase_binding(
                    contract, binding, Path("journal.json"), forward_inputs=inputs
                )
            stale = object.__getattribute__(binding, "runtime")
            stale.contract_digest = "0" * 64
            with self.assertRaises(ProductionPhaseBindingError):
                build_production_phase_binding(contract, binding, Path("journal.json"))

    def test_factory_rejects_backend_adapter_and_engine_construction_failures(self) -> None:
        binding = object.__new__(LiveUpgradeBinding)
        object.__setattr__(binding, "adapter", object())
        with (
            patch.object(LiveUpgradeBinding, "is_admitted", return_value=True),
            self.assertRaises(ProductionPhaseBindingError),
        ):
            BoundProductionBackendAdapter(binding)
        contract, valid_binding, inputs = self._factory_forward_fixture()
        with (
            patch.object(LiveUpgradeBinding, "is_admitted", return_value=True),
            patch.object(LiveUpgradeBinding, "matches_contract", return_value=True),
            patch(
                "tools.production_phase_engine.UpgradeEngine", side_effect=TypeError("bad engine")
            ),
            self.assertRaises(ProductionPhaseBindingError),
        ):
            build_production_phase_binding(
                contract, valid_binding, Path("journal.json"), forward_inputs=inputs
            )

    def test_forward_capability_binding_rejects_each_operation_boundary(self) -> None:
        contract, binding, inputs = self._factory_forward_fixture()
        operations = _phase_operations(contract)
        with patch.object(LiveUpgradeBinding, "is_admitted", return_value=True):
            with self.assertRaises(ProductionPhaseBindingError):
                bind_forward_phase_capabilities(binding, operations, cast(Any, object()))
            missing = dict(operations)
            missing["backup"] = cast(Any, None)
            with self.assertRaises(ProductionPhaseBindingError):
                bind_forward_phase_capabilities(binding, missing, inputs)
            foreign = dict(operations)
            foreign["stage"] = dict(operations["stage"], operation_id="foreign")
            with self.assertRaises(ProductionPhaseBindingError):
                bind_forward_phase_capabilities(binding, foreign, inputs)
            with (
                patch(
                    "tools.production_phase_engine.BoundBackupPhaseAdapter",
                    side_effect=TypeError("rejected"),
                ),
                self.assertRaises(ProductionPhaseBindingError),
            ):
                bind_forward_phase_capabilities(binding, operations, inputs)
            no_commit = replace(inputs, commit=None)
            with patch.object(LiveUpgradeBinding, "matches_contract", return_value=True):
                build_production_phase_binding(
                    contract, binding, Path("journal.json"), forward_inputs=no_commit
                )

    def test_commit_admission_identity_and_backend_constructor_reject(self) -> None:
        _contract_value, _binding, inputs = self._factory_forward_fixture()
        commit = inputs.commit
        assert commit is not None
        bad_admission = replace(commit.admission, backend="git")
        with self.assertRaises(ProductionPhaseBindingError):
            _validate_forward_commit_evidence(inputs.engine_context, bad_admission, commit.evidence)
        with (
            patch.object(LiveUpgradeBinding, "is_admitted", return_value=False),
            self.assertRaises(ProductionPhaseBindingError),
        ):
            BoundProductionBackendAdapter(object.__new__(LiveUpgradeBinding))


if __name__ == "__main__":
    unittest.main()
