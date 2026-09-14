# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Fail-closed admission predicates for a coordinator upgrade."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping


class AdmissionError(ValueError):
    """Raised when an upgrade safety predicate is absent or false."""


PREFLIGHT_PREDICATES = (
    "release_authentic",
    "runtime_supported",
    "state_clean",
    "state_synchronized",
    "no_divergence",
    "no_active_leases",
    "no_wrapped_commands",
    "no_reconciliation",
    "no_publication",
    "binding_valid",
    "backend_valid",
    "vendor_valid",
    "disk_capacity_ok",
    "scratch_capacity_ok",
    "backup_destination_restorable",
)

QUIESCENCE_PREDICATES = (
    "maintenance_barrier",
    "workers_drained",
    "leases_fenced",
    "wrapped_commands_drained",
    "reconciliation_stopped",
    "publication_stopped",
)

REOPEN_PREDICATES = (
    "maintenance_barrier",
    "runtime_validated",
    "backend_roundtrip_valid",
    "projections_valid",
    "binding_valid",
    "lease_fence_valid",
)

IDENTITY_FIELDS = (
    "operation_id",
    "project_id",
    "backend",
    "state_revision",
    "fencing_token",
    "fencing_owner",
    "authority_revision",
    "barrier_identity_digest",
    "envelope_digest",
    "target",
)
QUIESCENCE_IDENTITY = "durable_barrier_id"
KNOWN_FIELDS = set(PREFLIGHT_PREDICATES + QUIESCENCE_PREDICATES + REOPEN_PREDICATES)
KNOWN_FIELDS.update((*IDENTITY_FIELDS, QUIESCENCE_IDENTITY, "validation_failed", "safe_mode_ready"))


def _valid_identity(field: str, value: object) -> bool:
    if field == "state_revision":
        return isinstance(value, int) and not isinstance(value, bool) and value >= 1
    if not isinstance(value, str) or not value:
        return False
    if field in {"barrier_identity_digest", "envelope_digest"}:
        return re.fullmatch(r"[0-9a-f]{64}", value) is not None
    if field == "backend":
        return value in {"git", "sqlite"}
    if field == "project_id":
        try:
            return uuid.UUID(value).version == 4
        except ValueError:
            return False
    if field == "target":
        return value in {"new", "rollback"}
    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is not None


def _require(snapshot: Mapping[str, object], predicates: tuple[str, ...], phase: str) -> None:
    unknown = set(snapshot) - KNOWN_FIELDS
    if unknown:
        raise AdmissionError(f"{phase} denied; unknown fields: {', '.join(sorted(unknown))}")
    for field in IDENTITY_FIELDS:
        if not _valid_identity(field, snapshot.get(field)):
            raise AdmissionError(f"{phase} denied; invalid {field}")
    missing = [name for name in predicates if snapshot.get(name) is not True]
    if missing:
        raise AdmissionError(f"{phase} denied; unmet predicates: {', '.join(missing)}")


def admit_preflight(snapshot: Mapping[str, object]) -> None:
    """Allow staging only when every read-only prerequisite is proven."""
    _require(snapshot, PREFLIGHT_PREDICATES, "preflight")


def admit_quiesced(snapshot: Mapping[str, object]) -> None:
    """Allow replacement only while the durable maintenance barrier is held."""
    _require(snapshot, QUIESCENCE_PREDICATES, "quiescence")
    barrier = snapshot.get(QUIESCENCE_IDENTITY)
    if not isinstance(barrier, str) or not barrier:
        raise AdmissionError("quiescence denied; durable barrier proof is absent")


def recheck_before_replacement(
    admitted: Mapping[str, object], current: Mapping[str, object]
) -> None:
    """Atomically recheck identity and quiescence immediately before replacement."""
    admit_quiesced(admitted)
    admit_quiesced(current)
    for field in (*IDENTITY_FIELDS, QUIESCENCE_IDENTITY):
        if admitted.get(field) != current.get(field):
            raise AdmissionError(f"replacement denied; stale {field}")


def admit_reopen(snapshot: Mapping[str, object]) -> None:
    """Allow work to reopen only after target or rollback integrity validation."""
    _require(snapshot, REOPEN_PREDICATES, "reopen")
    target = snapshot.get("target")
    if target not in {"new", "rollback"}:
        raise AdmissionError("reopen denied; target must be new or rollback")
    barrier = snapshot.get(QUIESCENCE_IDENTITY)
    if not isinstance(barrier, str) or not barrier:
        raise AdmissionError("reopen denied; durable barrier proof is absent")
    validation_failed = snapshot.get("validation_failed", False)
    if not isinstance(validation_failed, bool):
        raise AdmissionError("reopen denied; validation_failed must be boolean")
    if validation_failed:
        raise AdmissionError("reopen denied; failed validation requires safe mode")


def admit_safe_mode(snapshot: Mapping[str, object]) -> None:
    """Require an explicit safe-mode record when neither runtime can reopen."""
    _require(snapshot, (), "safe mode")
    barrier = snapshot.get(QUIESCENCE_IDENTITY)
    if not isinstance(barrier, str) or not barrier:
        raise AdmissionError("safe mode denied; durable barrier proof is absent")
    safe_mode_ready = snapshot.get("safe_mode_ready")
    if not isinstance(safe_mode_ready, bool):
        raise AdmissionError("safe mode denied; safe_mode_ready must be boolean")
    if not safe_mode_ready:
        raise AdmissionError("safe mode denied; durable safe-mode record is absent")
