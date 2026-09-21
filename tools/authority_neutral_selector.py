# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Authority-neutral admission evidence for a future selector publication."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, cast


class SelectorAdmissionExecutionError(RuntimeError):
    """Raised when selector publication admission cannot be proven safely."""


class SelectorAdmissionBackend(Protocol):
    """Read-only selector admission operations owned by a concrete backend."""

    def selector_visibility_scope(
        self,
        selector_ref: str,
        expected_state_revision: int,
        barrier_id: str,
        fencing_token: str,
        *,
        selector_root: Path,
    ) -> Any: ...


_REQUIRED_CONTEXT = frozenset(
    {
        "backend",
        "target",
        "operation_id",
        "fencing_token",
        "selector_ref",
        "expected_state_revision",
        "durable_barrier_id",
        "selector_root",
        "binding",
    }
)
_IDENTITY_FIELDS = frozenset(
    {
        "backend",
        "target",
        "operation_id",
        "fencing_token",
        "selector_ref",
        "expected_state_revision",
        "durable_barrier_id",
        "selector_root",
    }
)


def _validate_context(context: Mapping[str, object]) -> None:  # noqa: C901
    if not isinstance(context, Mapping) or not set(context) >= _REQUIRED_CONTEXT:
        raise SelectorAdmissionExecutionError("selector admission context is incomplete")
    if context["backend"] != "sqlite" or context["target"] != "new":
        raise SelectorAdmissionExecutionError("selector admission context identity is invalid")
    if not isinstance(context["operation_id"], str) or not context["operation_id"]:
        raise SelectorAdmissionExecutionError("selector admission operation identity is invalid")
    if not isinstance(context["fencing_token"], str) or not context["fencing_token"]:
        raise SelectorAdmissionExecutionError("selector admission fencing identity is invalid")
    if not isinstance(context["selector_ref"], str) or not context["selector_ref"]:
        raise SelectorAdmissionExecutionError("selector admission selector identity is invalid")
    if (
        type(context["expected_state_revision"]) is not int
        or context["expected_state_revision"] < 1
    ):
        raise SelectorAdmissionExecutionError("selector admission revision is invalid")
    if not isinstance(context["durable_barrier_id"], str) or not context["durable_barrier_id"]:
        raise SelectorAdmissionExecutionError("selector admission barrier identity is invalid")
    if not isinstance(context["selector_root"], str) or not context["selector_root"]:
        raise SelectorAdmissionExecutionError("selector admission root is invalid")
    if not isinstance(context["binding"], Mapping):
        raise SelectorAdmissionExecutionError("selector admission binding is invalid")


def execute_verified_selector_admission(
    backend: SelectorAdmissionBackend,
    operation: Mapping[str, object],
    context: Mapping[str, object],
) -> dict[str, object]:
    """Prove selector publication admission without publishing anything."""
    if not isinstance(operation, Mapping) or operation.get("opcode") != "selector.admit":
        raise SelectorAdmissionExecutionError("selector admission operation is unsupported")
    _validate_context(context)
    try:
        root = Path(cast(str, context["selector_root"]))
        with backend.selector_visibility_scope(
            cast(str, context["selector_ref"]),
            cast(int, context["expected_state_revision"]),
            cast(str, context["durable_barrier_id"]),
            cast(str, context["fencing_token"]),
            selector_root=root,
        ) as snapshot:
            if snapshot is None:
                raise SelectorAdmissionExecutionError("selector admission snapshot is absent")
    except SelectorAdmissionExecutionError:
        raise
    except Exception as error:
        raise SelectorAdmissionExecutionError("selector admission recheck failed") from error
    return {
        "outcome": "completed",
        "selector_admission_verified": True,
        "selector_before_verified": True,
        "selector_after_verified": False,
        "selector_commit_atomic": False,
        "backend_identity_verified": True,
        "mutates_authority": False,
        "fencing_token": context["fencing_token"],
    }


class BoundSelectorAdmissionAdapter:
    """Bind selector admission to one immutable operation/session identity."""

    def __init__(
        self,
        backend: SelectorAdmissionBackend,
        operation: Mapping[str, object],
        context: Mapping[str, object],
    ) -> None:
        if not isinstance(operation, Mapping) or operation.get("opcode") != "selector.admit":
            raise SelectorAdmissionExecutionError("selector admission operation is unsupported")
        _validate_context(context)
        self._backend = backend
        self._operation = MappingProxyType(deepcopy(dict(operation)))
        self._context = MappingProxyType(deepcopy(dict(context)))
        self._identity = {field: self._context[field] for field in _IDENTITY_FIELDS}

    def execute(self, phase: str, context: Mapping[str, object]) -> dict[str, object]:
        if phase != "selector_admission":
            raise SelectorAdmissionExecutionError("selector admission phase is unsupported")
        if any(context.get(field) != value for field, value in self._identity.items()):
            raise SelectorAdmissionExecutionError("selector admission identity mismatch")
        return execute_verified_selector_admission(self._backend, self._operation, self._context)
