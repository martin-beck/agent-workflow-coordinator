# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Deterministic, mutation-free integrated transition rehearsal."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from types import MappingProxyType
from typing import Any


class RehearsalExecutionError(RuntimeError):
    """Raised when integrated transition evidence is incomplete or unsafe."""


PHASE_ORDER = (
    "backup",
    "stage",
    "validate",
    "selector_admission",
    "runtime_admission",
    "commit_admission",
    "recovery_admission",
)
_IDENTITY_FIELDS = ("backend", "operation_id", "fencing_token")
_PHASE_PROOFS = {
    "backup": ("backup_verified", "restore_roundtrip_verified"),
    "stage": ("staged_verified", "manifest_verified"),
    "validate": ("runtime_validated",),
    "selector_admission": ("selector_admission_verified",),
    "runtime_admission": ("runtime_replacement_admission_verified",),
    "commit_admission": ("commit_prerequisites_verified",),
    "recovery_admission": ("recovery_evidence_verified",),
}


def _validate_record(  # noqa: C901
    phase: str, record: Mapping[str, object], identity: Mapping[str, object]
) -> None:
    if not isinstance(record, Mapping) or record.get("phase") != phase:
        raise RehearsalExecutionError(f"rehearsal phase {phase} is missing or out of order")
    for field in _IDENTITY_FIELDS:
        if record.get(field) != identity[field]:
            raise RehearsalExecutionError(f"rehearsal phase {phase} identity drifted")
    if record.get("outcome") != "completed":
        if record.get("outcome") not in {"failed", "ambiguous"}:
            raise RehearsalExecutionError(f"rehearsal phase {phase} outcome is invalid")
        if record.get("functional_available") is not True or record.get("write_closed") is not True:
            raise RehearsalExecutionError(f"rehearsal phase {phase} failure is unsafe")
        if (
            record.get("outcome") == "ambiguous"
            and record.get("reconciliation_required") is not True
        ):
            raise RehearsalExecutionError(f"rehearsal phase {phase} ambiguity is unreconciled")
        return
    if record.get("mutates_authority") is not False:
        raise RehearsalExecutionError(f"rehearsal phase {phase} is mutating")
    for proof in _PHASE_PROOFS[phase]:
        if record.get(proof) is not True:
            raise RehearsalExecutionError(f"rehearsal phase {phase} lacks {proof}")


def execute_integrated_rehearsal(
    records: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Validate the complete bounded transition trace without dispatching mutation."""
    if not isinstance(records, Sequence):
        raise RehearsalExecutionError("rehearsal records are invalid")
    if len(records) != len(PHASE_ORDER):
        raise RehearsalExecutionError("rehearsal phase trace is incomplete")
    first = records[0]
    if not isinstance(first, Mapping):
        raise RehearsalExecutionError("rehearsal identity is unavailable")
    identity = {field: first.get(field) for field in _IDENTITY_FIELDS}
    if any(not isinstance(value, str) or not value for value in identity.values()):
        raise RehearsalExecutionError("rehearsal identity is invalid")
    for phase, record in zip(PHASE_ORDER, records, strict=True):
        _validate_record(phase, record, identity)
    return {
        "outcome": "completed",
        "phases_verified": list(PHASE_ORDER),
        "mutation_dispatched": False,
        "functional_available": True,
        "write_closed": True,
        "reconciliation_required": any(record.get("outcome") == "ambiguous" for record in records),
        "backend": identity["backend"],
        "operation_id": identity["operation_id"],
        "fencing_token": identity["fencing_token"],
    }


class BoundIntegratedRehearsal:
    """Retain one immutable evidence trace for a single rehearsal identity."""

    def __init__(self, records: Sequence[Mapping[str, object]]) -> None:
        result = execute_integrated_rehearsal(records)
        self._records = tuple(MappingProxyType(deepcopy(dict(record))) for record in records)
        self._identity = {field: result[field] for field in _IDENTITY_FIELDS}

    def execute(self, records: Sequence[Mapping[str, object]]) -> dict[str, Any]:
        result = execute_integrated_rehearsal(records)
        if any(result[field] != value for field, value in self._identity.items()):
            raise RehearsalExecutionError("rehearsal identity mismatch")
        return result
