# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Revision-bound public events for a reusable discussion session.

Coordinator validates identity, ordering, and revision fences only.  It stores
the response reference and unresolved state without interpreting the user's
decision.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from enum import StrEnum

PUBLIC_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
TASK_ID = re.compile(r"AR-\d{4}")
EVENT_ACTIONS = ("enter", "focus", "respond", "unresolved")


class SessionError(ValueError):
    """A malformed, stale, duplicated, or out-of-order session event."""


class SessionActor(StrEnum):
    AGENT = "agent"
    USER = "user"


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """One immutable event carrying all cross-project identity anchors."""

    event_id: str
    task_id: str
    task_revision: int
    session_id: str
    packet_ref: str
    point_ref: str
    anchor_ref: str
    action: str
    actor: SessionActor
    response_ref: str | None
    recorded_at: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.event_id, "event_id"),
            (self.session_id, "session_id"),
            (self.packet_ref, "packet_ref"),
            (self.point_ref, "point_ref"),
            (self.anchor_ref, "anchor_ref"),
        ):
            _public_ref(value, label)
        if not TASK_ID.fullmatch(self.task_id):
            raise SessionError("task_id is invalid")
        if type(self.task_revision) is not int or self.task_revision < 1:
            raise SessionError("task_revision must be positive")
        if self.action not in EVENT_ACTIONS:
            raise SessionError("session action is invalid")
        if self.response_ref is not None:
            _public_ref(self.response_ref, "response_ref")
        try:
            parsed = dt.datetime.fromisoformat(self.recorded_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise SessionError("recorded_at is invalid") from error
        if parsed.tzinfo is None:
            raise SessionError("recorded_at requires a timezone")


@dataclass(slots=True)
class SessionState:
    """Bounded Coordinator-owned state for one discussion session."""

    task_id: str
    task_revision: int
    session_id: str | None = None
    active_point: str | None = None
    anchor_ref: str | None = None
    entered: bool = False
    responded: bool = False
    unresolved: bool = False
    event_ids: set[str] = field(default_factory=set)

    def apply(self, event: SessionEvent) -> None:  # noqa: C901
        """Apply one event after checking all session and task fences."""
        if event.task_id != self.task_id:
            raise SessionError("event task_id does not match session")
        if event.event_id in self.event_ids:
            raise SessionError("duplicate session event")
        if event.task_revision != self.task_revision:
            raise SessionError("stale session event revision")
        if self.session_id is not None and event.session_id != self.session_id:
            raise SessionError("stale session identity")
        if self.anchor_ref is not None and event.anchor_ref != self.anchor_ref:
            raise SessionError("event anchor does not match session")
        if event.action == "enter":
            if self.entered:
                raise SessionError("session entry already recorded")
            self.session_id = event.session_id
            self.active_point = event.point_ref
            self.anchor_ref = event.anchor_ref
            self.entered = True
        elif not self.entered:
            raise SessionError("session event skipped entry gate")
        elif event.action == "focus":
            self.active_point = event.point_ref
        elif event.action == "respond":
            if event.response_ref is None:
                raise SessionError("response event requires response_ref")
            if event.point_ref != self.active_point:
                raise SessionError("response point does not match active point")
            self.responded = True
            self.unresolved = False
        else:
            if event.point_ref != self.active_point:
                raise SessionError("unresolved point does not match active point")
            self.unresolved = True
        self.event_ids.add(event.event_id)
        self.task_revision += 1


def _public_ref(value: str, label: str) -> None:
    if not isinstance(value, str) or not PUBLIC_REF.fullmatch(value):
        raise SessionError(f"{label} must be public-safe")
    if value.startswith("/") or ".." in value or "//" in value:
        raise SessionError(f"{label} must be public-safe")
