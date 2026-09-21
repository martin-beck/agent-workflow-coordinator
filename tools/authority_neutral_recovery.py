# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Authority-neutral diagnostic evidence for known-good recovery."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from types import MappingProxyType
from typing import Any


class RecoveryAuthorizationExecutionError(RuntimeError):
    """Raised when known-good recovery evidence is absent or unsafe."""


_IDENTITY_FIELDS = frozenset(
    {"backend", "target", "operation_id", "fencing_token", "state_revision", "barrier_id"}
)
_REQUIRED_EVIDENCE = frozenset(
    {
        "backend",
        "target",
        "operation_id",
        "fencing_token",
        "state_revision",
        "barrier_id",
        "backup_verified",
        "restore_roundtrip_verified",
        "runtime_validated",
        "backend_identity_verified",
        "mutates_authority",
        "rollback_context_verified",
    }
)


def _validate_evidence(evidence: Mapping[str, object]) -> None:  # noqa: C901
    if not isinstance(evidence, Mapping) or not set(evidence) >= _REQUIRED_EVIDENCE:
        raise RecoveryAuthorizationExecutionError("recovery evidence is incomplete")
    if evidence["backend"] not in {"git", "sqlite"} or evidence["target"] != "rollback":
        raise RecoveryAuthorizationExecutionError("recovery evidence identity is invalid")
    for field in ("operation_id", "fencing_token", "barrier_id"):
        if not isinstance(evidence[field], str) or not evidence[field]:
            raise RecoveryAuthorizationExecutionError(f"recovery evidence {field} is invalid")
    if type(evidence["state_revision"]) is not int or evidence["state_revision"] < 1:
        raise RecoveryAuthorizationExecutionError("recovery evidence revision is invalid")
    for field in (
        "backup_verified",
        "restore_roundtrip_verified",
        "runtime_validated",
        "backend_identity_verified",
    ):
        if evidence[field] is not True:
            raise RecoveryAuthorizationExecutionError("recovery prerequisite is unverified")
    if evidence["mutates_authority"] is not False:
        raise RecoveryAuthorizationExecutionError("recovery evidence is mutating")
    if evidence["rollback_context_verified"] is not False:
        raise RecoveryAuthorizationExecutionError("recovery evidence is authorizing")


def execute_verified_recovery_evidence(
    operation: Mapping[str, object], evidence: Mapping[str, object]
) -> dict[str, object]:
    """Validate diagnostic recovery evidence without authorizing rollback."""
    if not isinstance(operation, Mapping) or operation.get("opcode") != "rollback.admit":
        raise RecoveryAuthorizationExecutionError("recovery operation is unsupported")
    _validate_evidence(evidence)
    return {
        "outcome": "completed",
        "recovery_evidence_verified": True,
        "rollback_authorized": False,
        "rollback_context_verified": False,
        "mutates_authority": False,
        "backend_identity_verified": True,
        "fencing_token": evidence["fencing_token"],
    }


class BoundRecoveryEvidenceAdapter:
    """Bind diagnostic recovery evidence to one immutable operation identity."""

    def __init__(self, operation: Mapping[str, object], evidence: Mapping[str, object]) -> None:
        if not isinstance(operation, Mapping) or operation.get("opcode") != "rollback.admit":
            raise RecoveryAuthorizationExecutionError("recovery operation is unsupported")
        _validate_evidence(evidence)
        self._operation = MappingProxyType(deepcopy(dict(operation)))
        self._evidence = MappingProxyType(deepcopy(dict(evidence)))
        self._identity = {field: self._evidence[field] for field in _IDENTITY_FIELDS}

    def execute(self, phase: str, evidence: Mapping[str, object]) -> dict[str, Any]:
        if phase != "recovery_admission":
            raise RecoveryAuthorizationExecutionError("recovery phase is unsupported")
        if any(evidence.get(field) != value for field, value in self._identity.items()):
            raise RecoveryAuthorizationExecutionError("recovery evidence identity mismatch")
        return execute_verified_recovery_evidence(self._operation, self._evidence)
