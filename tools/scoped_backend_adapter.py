# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Uncalled fail-closed adapter boundary for the upgrade engine."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from tools.admission_lease import AdmissionLease
from tools.admitted_control_store import OrderedAdmissionScope
from tools.lock_domain import LockDomainIdentity
from tools.rollback_control_store import BarrierSessionState

DISABLED_MUTATION_PHASES = frozenset({"commit", "apply", "rollback"})
_SCOPE_CONTEXT_FIELDS = frozenset(
    {
        "project_id",
        "authority_revision",
        "fencing_token",
        "fencing_owner",
        "durable_barrier_id",
        "state_revision",
    }
)


@runtime_checkable
class CallerTraceScope(OrderedAdmissionScope, Protocol):
    """Scope that binds each engine context to its immutable lease."""

    def assert_context(self, context: Mapping[str, object]) -> None: ...


@runtime_checkable
class BackendAdapter(Protocol):
    """Minimal upgrade-engine backend surface guarded by this boundary."""

    def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, Any]: ...

    def verify_rollback_context(self, context: Mapping[str, object]) -> dict[str, Any] | None: ...

    def execute(self, phase: str, context: Mapping[str, object]) -> dict[str, Any]: ...


class ScopedBackendAdapter:
    """Wrap backend operations in a caller-owned, proven admission scope.

    This adapter is a contract seam only.  Command dispatch does not construct
    it, and it does not make upgrade/apply/rollback executable.
    """

    def __init__(self, backend: BackendAdapter, scope: CallerTraceScope) -> None:
        if not isinstance(backend, BackendAdapter):
            raise TypeError("upgrade backend adapter is incomplete")
        if not isinstance(scope, CallerTraceScope):
            raise TypeError("caller trace scope is required")
        self._backend = backend
        self._scope = scope

    @classmethod
    def from_validated_session(
        cls,
        backend: BackendAdapter,
        scope: CallerTraceScope,
        identity: LockDomainIdentity,
        current_identity: object,
        session_state: BarrierSessionState,
        lease: AdmissionLease,
    ) -> ScopedBackendAdapter:
        """Construct only after descriptor and durable caller evidence match."""
        if not isinstance(identity, LockDomainIdentity):
            raise TypeError("lock-domain identity is required")
        scope.assert_ordered()
        identity.assert_descriptor_binding(current_identity)
        identity.assert_session_binding(session_state, lease)
        return cls(backend, scope)

    @classmethod
    def from_rechecked_session(
        cls,
        backend: BackendAdapter,
        scope: CallerTraceScope,
        identity: LockDomainIdentity,
        current_identity: object,
        session_state: BarrierSessionState,
        lease: AdmissionLease,
    ) -> ScopedBackendAdapter:
        """Recheck the live scope immediately before creating the seam object."""
        with scope.hold():
            return cls.from_validated_session(
                backend, scope, identity, current_identity, session_state, lease
            )

    def snapshot(
        self,
        phase: str,
        context: Mapping[str, object],
        *,
        scope_context: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        """Read backend evidence only while the scope is held."""
        if scope_context is not None:
            self._validate_scope_context(scope_context)
        self._scope.assert_context(context if scope_context is None else scope_context)
        with self._scope.hold():
            snapshot = self._backend.snapshot(phase, context)
        if not isinstance(snapshot, dict):
            raise TypeError("backend snapshot must be an object")
        return snapshot

    @staticmethod
    def _validate_scope_context(context: Mapping[str, object]) -> None:
        """Validate the exact reduced admission identity before entering scope."""
        if not isinstance(context, Mapping) or set(context) != _SCOPE_CONTEXT_FIELDS:
            raise TypeError("scope context must contain exactly the admission identity fields")
        for field in _SCOPE_CONTEXT_FIELDS - {"state_revision"}:
            value = context.get(field)
            if type(value) is not str or not value:
                raise TypeError("scope context identity values are invalid")
        revision = context.get("state_revision")
        if type(revision) is not int or revision < 1:
            raise TypeError("scope context revision is invalid")

    def execute(self, phase: str, context: Mapping[str, object]) -> dict[str, Any]:
        """Execute one adapter operation only while the scope is held."""
        if phase in DISABLED_MUTATION_PHASES:
            raise TypeError("mutation phase is disabled at scoped adapter boundary")
        self._scope.assert_context(context)
        with self._scope.hold():
            result = self._backend.execute(phase, context)
        if not isinstance(result, dict):
            raise TypeError("backend execution result must be an object")
        if result.get("mutates_authority") is not False:
            raise TypeError("scoped backend execution must be non-mutating")
        return result

    def verify_rollback_context(self, context: Mapping[str, object]) -> dict[str, Any] | None:
        """Verify rollback evidence only while the scope is held."""
        self._scope.assert_context(context)
        with self._scope.hold():
            result = self._backend.verify_rollback_context(context)
        if result is not None and not isinstance(result, dict):
            raise TypeError("rollback verification result must be an object or null")
        return result
