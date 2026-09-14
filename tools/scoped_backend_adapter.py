# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Uncalled fail-closed adapter boundary for the upgrade engine."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from tools.admitted_control_store import OrderedAdmissionScope


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

    def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, Any]:
        """Read backend evidence only while the scope is held."""
        self._scope.assert_context(context)
        with self._scope.hold():
            snapshot = self._backend.snapshot(phase, context)
        if not isinstance(snapshot, dict):
            raise TypeError("backend snapshot must be an object")
        return snapshot

    def execute(self, phase: str, context: Mapping[str, object]) -> dict[str, Any]:
        """Execute one adapter operation only while the scope is held."""
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
