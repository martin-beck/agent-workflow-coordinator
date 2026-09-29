# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Bind a generated upgrade contract to one admitted production session.

This module is a capability factory, not an authority mutator.  It makes the
identity and evidence boundary explicit while preserving the concrete Git and
SQLite adapters' fail-closed ``execute`` methods.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from tools.git_authority_adapter import GitAuthorityAdapter
from tools.sqlite_authority_adapter import SQLiteAuthorityAdapter
from tools.upgrade_binding import LiveUpgradeBinding, canonical_contract_digest
from tools.upgrade_contract_runtime import PHASES, validate_runtime_contract
from tools.upgrade_engine import (
    BoundRollbackCapability,
    PhaseContext,
    UpgradeEngine,
    UpgradeError,
)


class ProductionPhaseBindingError(ValueError):
    """A generated phase set cannot be bound to the admitted live identity."""


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


class BoundProductionBackendAdapter:
    """Revalidate the live binding before every backend observation.

    The wrapped adapter remains the owner of backend evidence.  In particular,
    this wrapper does not add an ``execute`` implementation and therefore
    cannot turn a read-only binding into an authority mutation capability.
    """

    requires_bound_rollback = True

    def __init__(self, binding: LiveUpgradeBinding) -> None:
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

    @property
    def operation_lock(self) -> Any:
        return getattr(self._adapter, "operation_lock", None)

    def _admit(self) -> None:
        if not self._binding.is_admitted():
            raise ProductionPhaseBindingError("live upgrade binding is no longer admitted")

    def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, Any]:
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

    def execute(self, phase: str, context: Mapping[str, object]) -> dict[str, Any]:
        self._admit()
        result = self._adapter.execute(phase, context)
        if not isinstance(result, dict):
            raise ProductionPhaseBindingError("backend execution result is not an object")
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


def build_production_phase_binding(
    contract: Mapping[str, object],
    live_binding: LiveUpgradeBinding,
    journal: Path,
    *,
    lock_path: Path | None = None,
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
    backend = BoundProductionBackendAdapter(live_binding)
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
            envelope,
            lock_path,
            backend_adapter=backend,
            rollback_bound_verifier=rollback,
        )
    except Exception as error:
        raise ProductionPhaseBindingError("production phase engine binding was rejected") from error
    return ProductionPhaseBinding(
        validated, operations, context, live_binding, backend, rollback, engine
    )
