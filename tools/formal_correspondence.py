# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Executable correspondence checks for backup lifecycle evidence traces."""

from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path

EVENT_ACTIONS = {
    "backup_verified": "ExecuteSuccess",
    "publication_failed": "ExecuteReject",
    "restore_failed": "ExecuteReject",
    "retry_verified": "ExecuteSuccess",
}
INVARIANT_PREDICATES = {
    "NoReplacementBeforeBackup": "ProjectionAtomicity",
    "AmbiguousIsWriteClosed": "LockSafety",
    "ReconcileRequiresFence": "RevisionAccounting",
}


def formal_provenance(root: Path) -> dict[str, object]:
    model = root / "formal/handoffctl/Handoffctl.tla"
    config = root / "formal/handoffctl/Handoffctl.cfg"
    text = model.read_text(encoding="utf-8")
    missing = [
        predicate for predicate in INVARIANT_PREDICATES.values() if f"{predicate} ==" not in text
    ]
    if missing:
        raise ValueError(f"formal invariant definitions missing: {', '.join(missing)}")
    return {
        "model": str(model),
        "model_sha256": sha256(model.read_bytes()).hexdigest(),
        "config": str(config),
        "config_sha256": sha256(config.read_bytes()).hexdigest(),
        "invariants": dict(INVARIANT_PREDICATES),
    }


def validate_trace(events: Sequence[str]) -> tuple[str, ...]:
    """Validate terminal lifecycle events and return their model actions."""
    if not events or events[-1] not in {
        "backup_verified",
        "retry_verified",
        "publication_failed",
        "restore_failed",
    }:
        raise ValueError("lifecycle trace must end in a terminal outcome")
    if any(event not in EVENT_ACTIONS for event in events):
        raise ValueError("lifecycle trace contains an unknown event")
    if any(
        event in {"publication_failed", "restore_failed"} and events[index + 1] != "retry_verified"
        for index, event in enumerate(events[:-1])
    ):
        raise ValueError("a failed lifecycle outcome must be followed only by retry")
    return tuple(EVENT_ACTIONS[event] for event in events)
