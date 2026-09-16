"""Executable correspondence checks for backup lifecycle evidence traces."""

from __future__ import annotations

from collections.abc import Sequence

EVENT_ACTIONS = {
    "backup_verified": "ExecuteSuccess",
    "publication_failed": "ExecuteReject",
    "restore_failed": "ExecuteReject",
    "retry_verified": "ExecuteSuccess",
}
INVARIANTS = ("NoReplacementBeforeBackup", "AmbiguousIsWriteClosed", "ReconcileRequiresFence")


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
