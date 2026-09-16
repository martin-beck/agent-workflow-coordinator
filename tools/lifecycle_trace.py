# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Immutable lifecycle observations for bounded formal correspondence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


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
