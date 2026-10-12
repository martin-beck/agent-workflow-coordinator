# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Canonical, privacy-bounded execution-envelope records."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_SHA256 = re.compile(r"[0-9a-f]{64}$")
_TASK = re.compile(r"AR-[0-9]{4}$")
_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REQUIRED = frozenset(
    {
        "schema_version",
        "execution_id",
        "task",
        "task_revision",
        "owner",
        "claim_expires",
        "worktree_key",
        "branch",
        "spec_ref",
        "spec_revision",
        "policy_digest",
        "argv_sha256",
        "timeout_seconds",
        "created_at",
    }
)
_RESULT_REQUIRED = frozenset(
    {"schema_version", "execution_id", "classification", "returncode", "recorded_at"}
)
_CLASSIFICATIONS = frozenset({"EXIT", "SUBPROCESS_TIMEOUT"})


class ExecutionEnvelopeError(ValueError):
    """Raised when an execution intent is incomplete or unsafe."""


def canonical_json(value: Mapping[str, object]) -> str:
    """Return the only durable JSON representation for an envelope."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def argv_digest(argv: list[str]) -> str:
    """Hash argv without retaining command contents in durable records."""
    if not argv or not all(isinstance(item, str) and item and "\0" not in item for item in argv):
        raise ExecutionEnvelopeError("argv must be non-empty bounded strings")
    return hashlib.sha256("\0".join(argv).encode()).hexdigest()


def validate_envelope(value: object) -> dict[str, Any]:  # noqa: C901
    """Validate an exact v1 immutable execution intent."""
    if not isinstance(value, Mapping) or set(value) != _REQUIRED:
        raise ExecutionEnvelopeError("execution envelope fields are invalid")
    result = dict(value)
    if result["schema_version"] != 1:
        raise ExecutionEnvelopeError("execution envelope schema version is invalid")
    try:
        parsed = uuid.UUID(str(result["execution_id"]))
    except (ValueError, TypeError, AttributeError) as error:
        raise ExecutionEnvelopeError("execution id is invalid") from error
    if str(parsed) != result["execution_id"]:
        raise ExecutionEnvelopeError("execution id must be canonical")
    if not isinstance(result["task"], str) or not _TASK.fullmatch(result["task"]):
        raise ExecutionEnvelopeError("execution task is invalid")
    if (
        isinstance(result["task_revision"], bool)
        or not isinstance(result["task_revision"], int)
        or result["task_revision"] < 1
    ):
        raise ExecutionEnvelopeError("execution task revision is invalid")
    if not isinstance(result["owner"], str) or not _OWNER.fullmatch(result["owner"]):
        raise ExecutionEnvelopeError("execution owner is invalid")
    for field in ("claim_expires", "worktree_key", "branch", "created_at"):
        if not isinstance(result[field], str) or not result[field] or "\n" in result[field]:
            raise ExecutionEnvelopeError(f"execution {field} is invalid")
    for field in ("policy_digest", "argv_sha256"):
        if not isinstance(result[field], str) or not _SHA256.fullmatch(result[field]):
            raise ExecutionEnvelopeError(f"execution {field} is invalid")
    if (
        not isinstance(result["timeout_seconds"], (int, float))
        or isinstance(result["timeout_seconds"], bool)
        or result["timeout_seconds"] <= 0
    ):
        raise ExecutionEnvelopeError("execution timeout is invalid")
    if (result["spec_ref"] is None) != (result["spec_revision"] is None):
        raise ExecutionEnvelopeError("execution spec binding is incomplete")
    return result


def validate_result(value: object) -> dict[str, Any]:
    """Validate an exact v1 completion receipt for one immutable intent."""
    if not isinstance(value, Mapping) or set(value) != _RESULT_REQUIRED:
        raise ExecutionEnvelopeError("execution result fields are invalid")
    result = dict(value)
    if result["schema_version"] != 1:
        raise ExecutionEnvelopeError("execution result schema version is invalid")
    try:
        parsed = uuid.UUID(str(result["execution_id"]))
    except (ValueError, TypeError, AttributeError) as error:
        raise ExecutionEnvelopeError("execution result id is invalid") from error
    if str(parsed) != result["execution_id"]:
        raise ExecutionEnvelopeError("execution result id must be canonical")
    if result["classification"] not in _CLASSIFICATIONS:
        raise ExecutionEnvelopeError("execution result classification is invalid")
    if isinstance(result["returncode"], bool) or not isinstance(result["returncode"], int):
        raise ExecutionEnvelopeError("execution result return code is invalid")
    if (
        not isinstance(result["recorded_at"], str)
        or not result["recorded_at"]
        or "\n" in result["recorded_at"]
    ):
        raise ExecutionEnvelopeError("execution result timestamp is invalid")
    return result


def record_path(root: Path, task_id: str) -> Path:
    """Return the tracked append-only history for one task's execution receipts."""
    if not _TASK.fullmatch(task_id):
        raise ExecutionEnvelopeError("execution record task is invalid")
    return root / "execution-envelopes" / f"{task_id}.jsonl"


def load_records(root: Path, task_id: str) -> list[dict[str, Any]]:
    """Load one execution history, accepting only exact intent or result shapes."""
    path = record_path(root, task_id)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        try:
            value = json.loads(line)
            record = (
                validate_envelope(value)
                if isinstance(value, Mapping) and "task" in value
                else validate_result(value)
            )
        except (json.JSONDecodeError, ExecutionEnvelopeError) as error:
            raise ExecutionEnvelopeError(f"invalid execution record line {number}") from error
        if "task" in record and record["task"] != task_id:
            raise ExecutionEnvelopeError(f"execution record line {number} has wrong task")
        records.append(record)
    return records


def append_record(root: Path, task_id: str, record: Mapping[str, object]) -> None:
    """Durably append an exact, privacy-bounded receipt before/after execution."""
    checked = validate_envelope(record) if "task" in record else validate_result(record)
    if "task" in checked and checked["task"] != task_id:
        raise ExecutionEnvelopeError("execution intent task differs from destination")
    path = record_path(root, task_id)
    existing = load_records(root, task_id)
    encoded = "".join(canonical_json(item) + "\n" for item in [*existing, checked])
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()
