# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Canonical, privacy-bounded execution-envelope records."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
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
