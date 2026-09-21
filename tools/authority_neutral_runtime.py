# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Authority-neutral admission evidence for future runtime replacement."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from tools.runtime_bootstrap import verify_runtime_manifest
from tools.upgrade_authority import AuthorityError, read_runtime_selector


class RuntimeAdmissionExecutionError(RuntimeError):
    """Raised when staged runtime replacement admission is not provable."""


_REQUIRED_CONTEXT = frozenset(
    {
        "backend",
        "target",
        "operation_id",
        "fencing_token",
        "runtime_root",
        "manifest_digest",
        "selector_root",
        "selector_ref",
        "expected_release",
        "binding",
    }
)
_IDENTITY_FIELDS = frozenset(
    {
        "backend",
        "target",
        "operation_id",
        "fencing_token",
        "runtime_root",
        "manifest_digest",
        "selector_root",
        "selector_ref",
        "expected_release",
    }
)


def _validate_context(context: Mapping[str, object]) -> None:
    if not isinstance(context, Mapping) or not set(context) >= _REQUIRED_CONTEXT:
        raise RuntimeAdmissionExecutionError("runtime admission context is incomplete")
    if context["backend"] not in {"git", "sqlite"} or context["target"] != "new":
        raise RuntimeAdmissionExecutionError("runtime admission context identity is invalid")
    for field in (
        "operation_id",
        "fencing_token",
        "runtime_root",
        "manifest_digest",
        "selector_root",
        "selector_ref",
        "expected_release",
    ):
        if not isinstance(context[field], str) or not context[field]:
            raise RuntimeAdmissionExecutionError(f"runtime admission {field} is invalid")
    if not isinstance(context["binding"], Mapping):
        raise RuntimeAdmissionExecutionError("runtime admission binding is invalid")


def execute_verified_runtime_admission(
    operation: Mapping[str, object], context: Mapping[str, object]
) -> dict[str, object]:
    """Verify staged runtime and selector identity without replacing either."""
    if not isinstance(operation, Mapping) or operation.get("opcode") != "runtime.replace.admit":
        raise RuntimeAdmissionExecutionError("runtime admission operation is unsupported")
    _validate_context(context)
    runtime_root = Path(cast(str, context["runtime_root"]))
    selector = Path(cast(str, context["selector_root"])) / cast(str, context["selector_ref"])
    try:
        if verify_runtime_manifest(runtime_root, cast(str, context["manifest_digest"])) is not True:
            raise RuntimeAdmissionExecutionError("runtime manifest verification is incomplete")
        selected = read_runtime_selector(selector)
        if selected.get("active_release") != context["expected_release"]:
            raise RuntimeAdmissionExecutionError("runtime selector release identity is invalid")
    except RuntimeAdmissionExecutionError:
        raise
    except (AuthorityError, OSError, RuntimeError, ValueError) as error:
        raise RuntimeAdmissionExecutionError("runtime admission verification failed") from error
    return {
        "outcome": "completed",
        "runtime_replacement_admission_verified": True,
        "runtime_before_verified": True,
        "replacement_atomic": False,
        "selector_verified": True,
        "backend_identity_verified": True,
        "mutates_authority": False,
        "fencing_token": context["fencing_token"],
    }


class BoundRuntimeAdmissionAdapter:
    """Bind runtime admission to one immutable operation/session identity."""

    def __init__(self, operation: Mapping[str, object], context: Mapping[str, object]) -> None:
        if not isinstance(operation, Mapping) or operation.get("opcode") != "runtime.replace.admit":
            raise RuntimeAdmissionExecutionError("runtime admission operation is unsupported")
        _validate_context(context)
        self._operation = MappingProxyType(deepcopy(dict(operation)))
        self._context = MappingProxyType(deepcopy(dict(context)))
        self._identity = {field: self._context[field] for field in _IDENTITY_FIELDS}

    def execute(self, phase: str, context: Mapping[str, object]) -> dict[str, Any]:
        if phase != "runtime_admission":
            raise RuntimeAdmissionExecutionError("runtime admission phase is unsupported")
        if any(context.get(field) != value for field, value in self._identity.items()):
            raise RuntimeAdmissionExecutionError("runtime admission identity mismatch")
        return execute_verified_runtime_admission(self._operation, self._context)
