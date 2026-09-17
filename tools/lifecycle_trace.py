# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Immutable lifecycle observations for bounded formal correspondence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

MODEL_ACTIONS = {
    "acquire": "Preflight",
    "quiesce": "Quiesce",
    "backup": "Backup",
    "stage": "Stage",
    "commit": "Commit",
    "validate": "Validate",
    "reopen": "Reopen",
    "rollback_started": "StartRollback",
    "rollback_verified": "VerifyRollback",
    "rollback_released": "ReleaseRollback",
}


@dataclass(frozen=True, slots=True, init=False)
class LifecycleEvent:
    phase: str
    revision: int
    owner: str
    lock: str
    project_id: str
    session_digest: str
    fencing_token: str
    _token: object = field(repr=False, compare=False)

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("lifecycle events are scope-issued")


def _issue_event(
    token: object,
    phase: str,
    revision: int,
    owner: str,
    lock: str,
    project_id: str,
    session_digest: str,
    fencing_token: str,
) -> LifecycleEvent:
    event = object.__new__(LifecycleEvent)
    object.__setattr__(event, "phase", phase)
    object.__setattr__(event, "revision", revision)
    object.__setattr__(event, "owner", owner)
    object.__setattr__(event, "lock", lock)
    object.__setattr__(event, "project_id", project_id)
    object.__setattr__(event, "session_digest", session_digest)
    object.__setattr__(event, "fencing_token", fencing_token)
    object.__setattr__(event, "_token", token)
    return event


class LifecycleObserver(Protocol):
    def __call__(self, event: LifecycleEvent) -> None: ...


def validate_model_trace(events: tuple[LifecycleEvent, ...]) -> tuple[str, ...]:
    """Map one scope-issued lifecycle trace to UpgradeRecovery actions.

    The private issuance token binds every event to the same trusted scope;
    callers cannot construct events directly or mix observations from scopes.
    """
    if not events:
        raise ValueError("lifecycle trace must not be empty")
    if any(not isinstance(event, LifecycleEvent) for event in events):
        raise ValueError("lifecycle trace contains an invalid event")
    token = events[0]._token
    if any(event._token is not token for event in events):
        raise ValueError("lifecycle trace mixes scope-issued events")
    try:
        return tuple(MODEL_ACTIONS[event.phase] for event in events)
    except KeyError as error:
        raise ValueError("lifecycle trace phase is not mapped to the model") from error
