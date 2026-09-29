# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Bind a generated upgrade contract to one admitted production session.

This module is a capability factory, not an authority mutator.  It makes the
identity and evidence boundary explicit while preserving the concrete Git and
SQLite adapters' fail-closed ``execute`` methods.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from tools.authority_neutral_backup import BoundBackupPhaseAdapter
from tools.authority_neutral_commit import CommitAdmissionBundle
from tools.authority_neutral_stage import BoundStagePhaseAdapter
from tools.authority_neutral_validation import BoundValidationPhaseAdapter
from tools.git_authority_adapter import GitAuthorityAdapter
from tools.production_effect_binding import (
    ProductionEffectBindingError,
    bind_durable_commit_capability,
)
from tools.runtime_bootstrap import DispatchAdmission
from tools.sqlite_authority_adapter import SQLiteAuthorityAdapter, SQLiteLifecycleExecutor
from tools.upgrade_binding import LiveUpgradeBinding, canonical_contract_digest
from tools.upgrade_contract_runtime import PHASES, validate_runtime_contract
from tools.upgrade_engine import (
    BoundRollbackCapability,
    PhaseContext,
    UpgradeEngine,
    UpgradeError,
)
from tools.upgrade_identity import canonical_barrier_digest, canonical_envelope_digest


class ProductionPhaseBindingError(ValueError):
    """A generated phase set cannot be bound to the admitted live identity."""


@dataclass(frozen=True, slots=True)
class ForwardPhaseCapabilityInputs:
    """Caller-issued evidence needed to bind forward read-only phases.

    The live binding remains the rollback child issued by the durable barrier;
    this separate target-``new`` context is derived and checked against the
    same target-neutral session identity.  No field is inferred from a path or
    from the generated contract.
    """

    engine_context: Mapping[str, object]
    backup_context: Mapping[str, object]
    stage_context: Mapping[str, object]
    validation_admission: DispatchAdmission
    validation_context: Mapping[str, object]
    commit: ForwardCommitCapabilityInputs | None = None
    readiness_evidence: Mapping[str, Mapping[str, object]] | None = None


@dataclass(frozen=True, slots=True)
class ForwardCommitCapabilityInputs:
    """Backend-owned commit admission and effect arguments."""

    admission: CommitAdmissionBundle
    session_revision: int
    admission_reread: Callable[[], Mapping[str, object]]
    argument: object
    evidence: Mapping[str, object]
    effect_journal: object
    runner: Any | None = None
    connector: Any | None = None


@dataclass(frozen=True, slots=True)
class ForwardPhaseCapabilities:
    """Identity-bound, non-mutating phase capabilities."""

    backup: BoundBackupPhaseAdapter
    stage: BoundStagePhaseAdapter
    validation: BoundValidationPhaseAdapter


def _validate_forward_context(
    binding: LiveUpgradeBinding, context: Mapping[str, object]
) -> dict[str, object]:
    """Validate a forward envelope against the admitted rollback session."""
    if not isinstance(context, Mapping):
        raise ProductionPhaseBindingError("forward engine context is invalid")
    try:
        value = dict(context)
        if value.get("target") != "new":
            raise ProductionPhaseBindingError("forward engine target must be new")
        if set(value) != {
            "schema_version",
            "backend",
            "project_id",
            "operation_id",
            "state_revision",
            "authority_revision",
            "fencing_token",
            "fencing_owner",
            "durable_barrier_id",
            "artifact_root",
            "source",
            "destination",
            "manifest",
            "selector_ref",
            "barrier_identity_digest",
            "target",
            "envelope_digest",
        }:
            raise ProductionPhaseBindingError("forward engine context fields are incomplete")
        rollback = binding.runtime.runtime_envelope
        shared = (
            "schema_version",
            "backend",
            "project_id",
            "operation_id",
            "state_revision",
            "authority_revision",
            "fencing_token",
            "fencing_owner",
            "durable_barrier_id",
            "artifact_root",
            "source",
            "destination",
            "manifest",
            "selector_ref",
        )
        if any(value[field] != rollback[field] for field in shared):
            raise ProductionPhaseBindingError("forward engine identity differs from live binding")
        if value["barrier_identity_digest"] != canonical_barrier_digest(value):
            raise ProductionPhaseBindingError("forward barrier identity digest is invalid")
        if value["envelope_digest"] != canonical_envelope_digest(value):
            raise ProductionPhaseBindingError("forward envelope digest is invalid")
        return value
    except KeyError as error:
        raise ProductionPhaseBindingError("forward engine identity is incomplete") from error


def bind_forward_phase_capabilities(
    binding: LiveUpgradeBinding,
    operations: Mapping[str, Mapping[str, object]],
    inputs: ForwardPhaseCapabilityInputs,
    backend: BoundProductionBackendAdapter | None = None,
) -> ForwardPhaseCapabilities:
    """Bind backup, stage, and validation to one exact target-new context.

    Commit and rollback effects remain separate capability seams.  This helper
    only binds the already read-only/verification phases and rejects any
    operation or context identity drift before constructing them.
    """
    if type(binding) is not LiveUpgradeBinding or not binding.is_admitted():
        raise ProductionPhaseBindingError("an admitted live upgrade binding is required")
    if type(inputs) is not ForwardPhaseCapabilityInputs:
        raise ProductionPhaseBindingError("forward phase inputs are invalid")
    context = _validate_forward_context(binding, inputs.engine_context)
    for phase in ("backup", "stage", "validate"):
        if not isinstance(operations.get(phase), Mapping):
            raise ProductionPhaseBindingError(f"generated {phase} operation is missing")
        operation = operations[phase]
        if operation.get("operation_id") != f"{context['operation_id']}:{phase}":
            raise ProductionPhaseBindingError(f"generated {phase} operation identity is invalid")
    try:
        shared_backend = backend or BoundProductionBackendAdapter(binding)
        backup = BoundBackupPhaseAdapter(
            shared_backend,
            operations["backup"],
            inputs.backup_context,
        )
        stage = BoundStagePhaseAdapter(
            shared_backend,
            operations["stage"],
            inputs.stage_context,
        )
        validation = BoundValidationPhaseAdapter(
            shared_backend,
            inputs.validation_admission,
            operations["validate"],
            inputs.validation_context,
        )
    except Exception as error:
        raise ProductionPhaseBindingError(
            "forward phase capability binding was rejected"
        ) from error
    return ForwardPhaseCapabilities(backup, stage, validation)


def _validate_forward_validation_inputs(
    engine_context: Mapping[str, object],
    admission: DispatchAdmission,
    validation_context: Mapping[str, object],
) -> None:
    if not isinstance(validation_context, Mapping):
        raise ProductionPhaseBindingError("validation context is invalid")
    for field, expected in engine_context.items():
        if validation_context.get(field) != expected:
            raise ProductionPhaseBindingError(
                f"validation context identity differs from engine context: {field}"
            )
    descriptor = validation_context.get("binding")
    identity = getattr(admission, "identity", None)
    release = getattr(identity, "release", None)
    manifest_digest = getattr(identity, "digest", None)
    if (
        not isinstance(descriptor, Mapping)
        or not isinstance(release, str)
        or not isinstance(manifest_digest, str)
        or descriptor.get("release") != release
        or descriptor.get("manifest_digest") != manifest_digest
    ):
        raise ProductionPhaseBindingError("validation admission identity is not bound")


def _validate_forward_commit_evidence(
    engine_context: Mapping[str, object],
    admission: CommitAdmissionBundle,
    evidence: Mapping[str, object],
) -> None:
    if not isinstance(evidence, Mapping):
        raise ProductionPhaseBindingError("commit evidence is invalid")
    admission_expected = {
        "backend": engine_context.get("backend"),
        "target": "new",
        "operation_id": f"{engine_context.get('operation_id')}:commit",
        "state_revision": engine_context.get("state_revision"),
        "barrier_id": engine_context.get("durable_barrier_id"),
        "fencing_token": engine_context.get("fencing_token"),
    }
    admission_identity = {
        "backend": admission.backend,
        "target": admission.target,
        "operation_id": admission.operation_id,
        "state_revision": admission.state_revision,
        "barrier_id": admission.barrier_id,
        "fencing_token": admission.fencing_token,
    }
    if admission_identity != admission_expected:
        raise ProductionPhaseBindingError("commit admission identity differs from engine context")
    for name in ("admitted_snapshot", "current_snapshot"):
        snapshot = evidence.get(name)
        if not isinstance(snapshot, Mapping):
            raise ProductionPhaseBindingError("commit evidence snapshot is invalid")
        for field, context_expected in engine_context.items():
            if field in snapshot and snapshot[field] != context_expected:
                raise ProductionPhaseBindingError(
                    f"commit evidence snapshot identity differs: {field}"
                )


@dataclass(frozen=True, slots=True)
class ProductionPhaseBinding:
    """The immutable result of binding one contract to one live session."""

    contract: Mapping[str, object]
    operations: Mapping[str, Mapping[str, object]]
    context: PhaseContext
    live_binding: LiveUpgradeBinding
    backend: BoundProductionBackendAdapter
    rollback: BoundRollbackCapability
    engine: UpgradeEngine
    forward_capabilities: ForwardPhaseCapabilities | None = None
    commit_capability: object | None = None


class BoundProductionBackendAdapter:
    """Revalidate the live binding before every backend observation.

    The wrapped adapter remains the owner of backend evidence.  In particular,
    this wrapper does not add an ``execute`` implementation and therefore
    cannot turn a read-only binding into an authority mutation capability.
    """

    requires_bound_rollback = True

    def __init__(
        self,
        binding: LiveUpgradeBinding,
        *,
        journal: Path | None = None,
        readiness_evidence: Mapping[str, Mapping[str, object]] | None = None,
        commit_admission_evidence: Mapping[str, object] | None = None,
    ) -> None:
        if type(binding) is not LiveUpgradeBinding or not binding.is_admitted():
            raise ProductionPhaseBindingError("an admitted live upgrade binding is required")
        if type(binding.adapter) is GitAuthorityAdapter:
            kind = "git"
        elif type(binding.adapter) is SQLiteAuthorityAdapter:
            kind = "sqlite"
        else:  # pragma: no cover - LiveUpgradeBinding.bind currently prevents this
            raise ProductionPhaseBindingError("unsupported concrete production adapter")
        self._binding = binding
        self._adapter: Any = binding.adapter
        self.bound_rollback_kind = kind
        self._engine_journal = journal.absolute() if journal is not None else None
        self._lifecycle_journal = (
            journal.with_name(f".{journal.name}.lifecycle.json").absolute()
            if journal is not None
            else None
        )
        self._readiness_evidence = {
            phase: dict(value) for phase, value in (readiness_evidence or {}).items()
        }
        self._commit_admission_evidence = (
            dict(commit_admission_evidence) if commit_admission_evidence is not None else None
        )
        self._sqlite_lifecycle_executor: SQLiteLifecycleExecutor | None = None
        if kind == "sqlite" and journal is not None:
            session_store = getattr(binding.scope, "_session_store", None)
            if session_store is not None:
                try:
                    self._sqlite_lifecycle_executor = SQLiteLifecycleExecutor.bind(
                        cast(SQLiteAuthorityAdapter, binding.adapter),
                        session_store,
                        self._lifecycle_journal or journal,
                    )
                except Exception as error:
                    raise ProductionPhaseBindingError(
                        "SQLite durable lifecycle executor binding was rejected"
                    ) from error

    @property
    def operation_lock(self) -> Any:
        return getattr(self._adapter, "operation_lock", None)

    def _admit(self) -> None:
        if not self._binding.is_admitted():
            raise ProductionPhaseBindingError("live upgrade binding is no longer admitted")

    def refresh_after_commit(self) -> None:
        """Advance Git observation identity after the bound commit effect."""
        if self.bound_rollback_kind != "git":
            return
        current_head = self._adapter._git("rev-parse", "--verify", "HEAD")
        if current_head == self._binding.expected_head:
            return
        try:
            self._adapter._git(
                "merge-base", "--is-ancestor", cast(str, self._binding.expected_head), current_head
            )
            refreshed = LiveUpgradeBinding.bind(
                self._binding.runtime,
                self._binding.session,
                self._binding.scope,
                self._binding.lease,
                self._binding.admission_recheck,
                self._binding.adapter,
                expected_git_repository=self._binding.expected_git_repository,
            )
        except Exception as error:
            raise ProductionPhaseBindingError(
                "Git post-commit binding refresh was rejected"
            ) from error
        if refreshed.expected_head != current_head:
            raise ProductionPhaseBindingError("Git post-commit head reread is inconsistent")
        self._binding = refreshed

    def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, Any]:  # noqa: C901
        self._admit()
        if self.bound_rollback_kind == "git":
            result = self._adapter.snapshot_bound(
                phase,
                context,
                self._binding.scope,
                lease=self._binding.lease,
                admission_recheck=self._binding.admission_recheck,
                expected_branch=cast(str, self._binding.expected_branch),
                expected_head=cast(str, self._binding.expected_head),
            )
        else:
            result = self._adapter.snapshot_bound(
                phase,
                context,
                self._binding.scope,
                lease=self._binding.lease,
                admission_recheck=self._binding.admission_recheck,
            )
        if not isinstance(result, dict):
            raise ProductionPhaseBindingError("backend snapshot is not an object")
        evidence = getattr(self, "_readiness_evidence", {}).get(phase)
        if evidence is not None and phase in {"discover", "preflight", "quiesce", "reopen"}:
            immutable = {field: result[field] for field in context if field in result}
            if any(evidence.get(field) != value for field, value in immutable.items()):
                raise ProductionPhaseBindingError(
                    f"readiness evidence identity differs for {phase}"
                )
            result.update(evidence)
            result["phase"] = phase
            result["backend"] = context["backend"]
            result["mutates_authority"] = False
            result["fencing_token"] = context["fencing_token"]
            if phase == "preflight":
                result["preflight_snapshot"] = {
                    key: value
                    for key, value in evidence.items()
                    if key not in {"capacity_verified", "preflight_admitted"}
                }
            elif phase == "quiesce":
                result["quiescence_snapshot"] = {
                    key: value
                    for key, value in evidence.items()
                    if key not in {"barrier_acquired", "fencing_verified"}
                }
            elif phase == "reopen":
                result["reopen_snapshot"] = {
                    key: value
                    for key, value in evidence.items()
                    if key not in {"validated", "barrier_held"}
                }
        if phase == "commit" and self._commit_admission_evidence is not None:
            for key in ("admitted_snapshot", "current_snapshot"):
                snapshot = self._commit_admission_evidence.get(key)
                if not isinstance(snapshot, Mapping):
                    raise ProductionPhaseBindingError(f"commit admission evidence is missing {key}")
            result.update(
                {
                    "admitted_snapshot": self._commit_admission_evidence["admitted_snapshot"],
                    "current_snapshot": self._commit_admission_evidence["current_snapshot"],
                }
            )
        return result

    def verify_rollback_context(self, context: Mapping[str, object]) -> dict[str, Any]:
        self._admit()
        if self.bound_rollback_kind == "git":
            result = self._adapter.verify_rollback_context_bound(
                context,
                self._binding.scope,
                lease=self._binding.lease,
                admission_recheck=self._binding.admission_recheck,
                expected_branch=cast(str, self._binding.expected_branch),
                expected_head=cast(str, self._binding.expected_head),
            )
        else:
            result = self._adapter.verify_rollback_context_bound(
                context,
                self._binding.scope,
                lease=self._binding.lease,
                admission_recheck=self._binding.admission_recheck,
            )
        if not isinstance(result, dict):
            raise ProductionPhaseBindingError("rollback evidence is not an object")
        return result

    def verify_rollback_context_bound(
        self,
        context: Mapping[str, object],
        _scope: object,
        *,
        lease: object,
        admission_recheck: object,
        expected_branch: str | None = None,
        expected_head: str | None = None,
    ) -> dict[str, Any]:
        """Expose the engine's trusted verifier name without widening authority."""
        if _scope is not self._binding.scope:
            raise ProductionPhaseBindingError("rollback scope identity changed")
        if (
            lease is not self._binding.lease
            or admission_recheck is not self._binding.admission_recheck
        ):
            raise ProductionPhaseBindingError("rollback admission identity changed")
        if (
            expected_branch != self._binding.expected_branch
            or expected_head != self._binding.expected_head
        ):
            raise ProductionPhaseBindingError("rollback Git identity changed")
        return self.verify_rollback_context(context)

    def _sync_lifecycle_journal(self, operation_id: str) -> None:
        """Project the engine's started backup record into the SQLite journal schema."""
        source = self._engine_journal
        destination = self._lifecycle_journal
        if source is None or destination is None:
            raise ProductionPhaseBindingError("SQLite lifecycle journal is not configured")
        try:
            document = json.loads(source.read_text(encoding="utf-8"))
            records = document.get("records")
            if not isinstance(records, list) or not records:
                raise ValueError("engine journal records are missing")
            record = records[-1]
            if (
                not isinstance(record, dict)
                or record.get("step_id") != f"{operation_id.replace(':', '.', 1)}"
                or record.get("phase") != "backup"
                or record.get("outcome") != "started"
            ):
                raise ValueError("engine backup record is not started")
            projected_record = dict(record)
            projected_record["step_id"] = operation_id
            projected = {
                "status": "running",
                "phase": "backup",
                "records": [projected_record],
            }
            descriptor, temporary = tempfile.mkstemp(
                prefix=".sqlite-lifecycle-", suffix=".json", dir=destination.parent
            )
            temporary_path = Path(temporary)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(json.dumps(projected, sort_keys=True) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary_path.replace(destination)
                directory = os.open(destination.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                temporary_path.unlink(missing_ok=True)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ProductionPhaseBindingError(
                "SQLite lifecycle journal projection was rejected"
            ) from error

    def execute(self, phase: str, context: Mapping[str, object]) -> dict[str, Any]:
        self._admit()
        if phase in {"discover", "preflight", "quiesce", "reopen"}:
            evidence = getattr(self, "_readiness_evidence", {}).get(phase)
            if evidence is None:
                raise ProductionPhaseBindingError(
                    f"admitted readiness evidence is missing for {phase}"
                )
            snapshot = self.snapshot(phase, context)
            result = {**snapshot, **evidence}
            result["phase"] = phase
            result["backend"] = context["backend"]
            result["mutates_authority"] = False
            result["fencing_token"] = context["fencing_token"]
            if phase == "preflight":
                result["preflight_snapshot"] = {
                    key: value
                    for key, value in evidence.items()
                    if key not in {"capacity_verified", "preflight_admitted"}
                }
            elif phase == "quiesce":
                result["quiescence_snapshot"] = {
                    key: value
                    for key, value in evidence.items()
                    if key not in {"barrier_acquired", "fencing_verified"}
                }
            elif phase == "reopen":
                result["reopen_snapshot"] = {
                    key: value
                    for key, value in evidence.items()
                    if key not in {"validated", "barrier_held"}
                }
            return result
        result = self._adapter.execute(phase, context)
        if not isinstance(result, dict):
            raise ProductionPhaseBindingError("backend execution result is not an object")
        return result

    def execute_generated_operation(
        self,
        operation: Mapping[str, object],
        destination: Path,
        binding: dict[str, Any],
    ) -> dict[str, Any]:
        """Consume a generated SQLite backup through its durable lifecycle executor."""
        self._admit()
        if self.bound_rollback_kind == "sqlite":
            executor = self._sqlite_lifecycle_executor
            if executor is None:
                raise ProductionPhaseBindingError("SQLite durable lifecycle executor is not bound")
            operation_id = operation.get("operation_id")
            if not isinstance(operation_id, str):
                raise ProductionPhaseBindingError("generated operation identity is invalid")
            self._sync_lifecycle_journal(operation_id)
            return executor.execute_generated_operation(operation, destination, binding)
        raise ProductionPhaseBindingError("generated operation is unsupported for Git")

    def execute_generated_backup(
        self, operation: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, Any]:
        """Dispatch the admitted Git backup opcode through the concrete adapter."""
        self._admit()
        if self.bound_rollback_kind != "git":
            raise ProductionPhaseBindingError("generated Git backup is unsupported for SQLite")
        backend_context = {key: value for key, value in context.items() if key != "binding"}
        result = self._adapter.execute_generated_backup(operation, backend_context)
        if not isinstance(result, dict):
            raise ProductionPhaseBindingError("generated Git backup result is not an object")
        return result


def _phase_operations(contract: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    phases = contract.get("phases")
    if not isinstance(phases, list) or len(phases) != len(PHASES):
        raise ProductionPhaseBindingError("generated phase list is incomplete")
    operations: dict[str, Mapping[str, object]] = {}
    operation_id = contract.get("operation_id")
    if not isinstance(operation_id, str):
        raise ProductionPhaseBindingError("generated operation identity is invalid")
    for phase, value in zip(PHASES, phases, strict=True):
        if not isinstance(value, Mapping) or value.get("id") != phase:
            raise ProductionPhaseBindingError("generated phase identity is invalid")
        operation = value.get("operation")
        if not isinstance(operation, Mapping):
            raise ProductionPhaseBindingError("generated phase operation is missing")
        if operation.get("operation_id") != f"{operation_id}:{phase}":
            raise ProductionPhaseBindingError("generated phase operation identity is invalid")
        operations[phase] = dict(operation)
    return operations


def build_production_phase_binding(  # noqa: C901
    contract: Mapping[str, object],
    live_binding: LiveUpgradeBinding,
    journal: Path,
    *,
    lock_path: Path | None = None,
    forward_inputs: ForwardPhaseCapabilityInputs | None = None,
) -> ProductionPhaseBinding:
    """Construct the bound engine without authorizing any mutation."""
    try:
        validated = validate_runtime_contract(dict(contract))
    except Exception as error:
        raise ProductionPhaseBindingError("generated upgrade contract is invalid") from error
    if type(live_binding) is not LiveUpgradeBinding or not live_binding.is_admitted():
        raise ProductionPhaseBindingError("live upgrade binding is not admitted")
    backend_name = validated["backend"]
    if not live_binding.matches_contract(validated, cast(str, backend_name)):
        raise ProductionPhaseBindingError("live binding does not match generated contract")
    if live_binding.runtime.contract_digest != canonical_contract_digest(validated):
        raise ProductionPhaseBindingError("live binding contract digest is stale")
    operations = _phase_operations(validated)
    envelope = dict(live_binding.runtime.runtime_envelope)
    try:
        context = PhaseContext(**cast(dict[str, Any], envelope))
    except (TypeError, UpgradeError) as error:
        raise ProductionPhaseBindingError("live runtime envelope is not a phase context") from error
    readiness_evidence = forward_inputs.readiness_evidence if forward_inputs is not None else None
    commit_admission_evidence = (
        forward_inputs.commit.evidence
        if forward_inputs is not None and forward_inputs.commit is not None
        else None
    )
    backend = BoundProductionBackendAdapter(
        live_binding,
        journal=journal,
        readiness_evidence=readiness_evidence,
        commit_admission_evidence=commit_admission_evidence,
    )
    forward_capabilities: ForwardPhaseCapabilities | None = None
    commit_capability: object | None = None
    engine_context: Mapping[str, object] = envelope
    engine_kwargs: dict[str, Any] = {}
    if forward_inputs is not None:
        forward_capabilities = bind_forward_phase_capabilities(
            live_binding, operations, forward_inputs, backend
        )
        engine_context = _validate_forward_context(live_binding, forward_inputs.engine_context)
        _validate_forward_validation_inputs(
            engine_context,
            forward_inputs.validation_admission,
            forward_inputs.validation_context,
        )
        engine_kwargs = {
            "backup_operation": operations["backup"],
            "backup_context": forward_inputs.backup_context,
            "stage_operation": operations["stage"],
            "stage_context": forward_inputs.stage_context,
            "validation_admission": forward_inputs.validation_admission,
            "validation_operation": operations["validate"],
            "validation_context": forward_inputs.validation_context,
        }
        if forward_inputs.commit is not None:
            commit_operation = operations["commit"]
            admission = forward_inputs.commit.admission
            if commit_operation.get("opcode") != "authority.atomic_replace":
                raise ProductionPhaseBindingError("generated commit operation is unsupported")
            if admission.operation_id != commit_operation.get("operation_id"):
                raise ProductionPhaseBindingError("commit admission operation identity is invalid")
            _validate_forward_commit_evidence(
                engine_context,
                admission,
                forward_inputs.commit.evidence,
            )
            commit_inputs = commit_operation.get("inputs")
            if not isinstance(commit_inputs, Mapping):
                raise ProductionPhaseBindingError("generated commit inputs are invalid")
            commit_context = {
                "backend": admission.backend,
                "target": "new",
                "selector_ref": commit_inputs.get("selector_ref"),
                "operation_id": admission.operation_id,
                "state_revision": admission.state_revision,
                "durable_barrier_id": admission.barrier_id,
                "fencing_token": admission.fencing_token,
            }
            try:
                commit_capability = bind_durable_commit_capability(
                    live_binding,
                    admission,
                    forward_inputs.commit.effect_journal,
                    session_revision=forward_inputs.commit.session_revision,
                    admission_reread=forward_inputs.commit.admission_reread,
                    runner=forward_inputs.commit.runner,
                    connector=forward_inputs.commit.connector,
                )
            except (ProductionEffectBindingError, TypeError, ValueError) as error:
                raise ProductionPhaseBindingError(
                    "forward commit capability binding was rejected"
                ) from error
            engine_kwargs.update(
                commit_operation=commit_operation,
                commit_context=commit_context,
                commit_executor=commit_capability,
                commit_argument=forward_inputs.commit.argument,
                commit_evidence=forward_inputs.commit.evidence,
            )
    try:
        rollback = BoundRollbackCapability.bind(
            context,
            backend,
            live_binding.scope,
            lease=live_binding.lease,
            admission_recheck=live_binding.admission_recheck,
            expected_branch=live_binding.expected_branch,
            expected_head=live_binding.expected_head,
        )
        engine = UpgradeEngine(
            cast(str, validated["operation_id"]),
            journal,
            engine_context,
            lock_path,
            backend_adapter=backend,
            rollback_bound_verifier=rollback,
            **engine_kwargs,
        )
    except Exception as error:
        raise ProductionPhaseBindingError("production phase engine binding was rejected") from error
    return ProductionPhaseBinding(
        validated,
        operations,
        context,
        live_binding,
        backend,
        rollback,
        engine,
        forward_capabilities,
        commit_capability,
    )
