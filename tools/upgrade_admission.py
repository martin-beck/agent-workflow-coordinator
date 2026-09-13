# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Fail-closed admission predicates for a coordinator upgrade."""

from __future__ import annotations

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


def _require(snapshot: Mapping[str, object], predicates: tuple[str, ...], phase: str) -> None:
    missing = [name for name in predicates if snapshot.get(name) is not True]
    if missing:
        raise AdmissionError(f"{phase} denied; unmet predicates: {', '.join(missing)}")


def admit_preflight(snapshot: Mapping[str, object]) -> None:
    """Allow staging only when every read-only prerequisite is proven."""
    _require(snapshot, PREFLIGHT_PREDICATES, "preflight")


def admit_quiesced(snapshot: Mapping[str, object]) -> None:
    """Allow replacement only while the durable maintenance barrier is held."""
    _require(snapshot, QUIESCENCE_PREDICATES, "quiescence")


def admit_reopen(snapshot: Mapping[str, object]) -> None:
    """Allow work to reopen only after target or rollback integrity validation."""
    _require(snapshot, REOPEN_PREDICATES, "reopen")
