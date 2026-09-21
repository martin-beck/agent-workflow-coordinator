# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Authority-neutral, backup-only upgrade execution seam.

This module deliberately stops after an independently verified backup.  It does
not publish a selector, stage a runtime, commit authority state, or restore a
backup.  Those operations remain separately gated by the upgrade contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, cast


class BackupExecutionError(RuntimeError):
    """A backup-only operation cannot be proven safe."""


class BackupExecutor(Protocol):
    """Minimal generated-backup surface implemented by concrete adapters."""

    def execute_generated_backup(
        self, operation: Mapping[str, object], context: Mapping[str, object]
    ) -> Mapping[str, object]: ...

    def execute_generated_operation(
        self,
        operation: Mapping[str, object],
        destination: Path,
        binding: dict[str, Any],
    ) -> Mapping[str, object]: ...


_REQUIRED_CONTEXT = {
    "backend",
    "target",
    "destination",
    "artifact_root",
    "binding",
}
_REQUIRED_RESULT = {
    "outcome",
    "backup_verified",
    "restore_roundtrip_verified",
    "backend_identity_verified",
}


def _validated_artifacts(context: Mapping[str, object]) -> tuple[str, Path, dict[str, Any]]:
    if not isinstance(context, Mapping) or set(context) < _REQUIRED_CONTEXT:
        raise BackupExecutionError("backup execution context is incomplete")
    backend = context.get("backend")
    target = context.get("target")
    destination = context.get("destination")
    artifact_root = context.get("artifact_root")
    binding = context.get("binding")
    if backend not in {"git", "sqlite"} or target != "new":
        raise BackupExecutionError("backup execution identity is invalid")
    if not isinstance(destination, str) or not isinstance(artifact_root, str):
        raise BackupExecutionError("backup artifact binding is invalid")
    if not isinstance(binding, dict):
        raise BackupExecutionError("backup authority binding is invalid")
    destination_path = Path(destination).resolve()
    try:
        destination_path.relative_to(Path(artifact_root).resolve())
    except ValueError as error:
        raise BackupExecutionError("backup destination escapes artifact root") from error
    return backend, destination_path, cast(dict[str, Any], binding)


def _dispatch_backup(
    adapter: object,
    backend: str,
    operation: Mapping[str, object],
    context: Mapping[str, object],
    destination: Path,
    binding: dict[str, Any],
) -> Mapping[str, object]:
    concrete = cast(BackupExecutor, adapter)
    if backend == "git":
        return concrete.execute_generated_backup(operation, context)
    return concrete.execute_generated_operation(operation, destination, binding)


def _validated_result(result: Mapping[str, object]) -> dict[str, object]:
    if set(result) < _REQUIRED_RESULT:
        raise BackupExecutionError("generated backup result is incomplete")
    if (
        result.get("outcome") != "completed"
        or result.get("backup_verified") is not True
        or result.get("restore_roundtrip_verified") is not True
        or result.get("backend_identity_verified") is not True
        or result.get("mutates_authority") is True
    ):
        raise BackupExecutionError("generated backup postconditions are not proven")
    return dict(result)


def execute_verified_backup(
    adapter: object,
    operation: Mapping[str, object],
    context: Mapping[str, object],
) -> dict[str, object]:
    """Execute only a generated backup operation through the selected backend.

    The concrete Git/SQLite generated executors own their barrier, journal,
    identity, and durability checks.  This wrapper prevents the upgrade engine
    from selecting a backend-specific mutation path and validates the common
    postcondition before returning evidence to the phase journal.
    """
    if not isinstance(operation, Mapping) or operation.get("opcode") != "backend.backup":
        raise BackupExecutionError("backup operation is unsupported")
    backend, destination_path, binding = _validated_artifacts(context)
    try:
        result = _dispatch_backup(adapter, backend, operation, context, destination_path, binding)
    except BackupExecutionError:
        raise
    except Exception as error:
        raise BackupExecutionError("generated backup execution failed") from error
    if not isinstance(result, Mapping):
        raise BackupExecutionError("generated backup result is invalid")
    return _validated_result(result)
