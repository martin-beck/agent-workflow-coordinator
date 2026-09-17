# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Revision-bound state for batched discussion packets."""

from __future__ import annotations

import re
from dataclasses import dataclass

PUBLIC_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


class BatchError(ValueError):
    """A malformed, stale, cross-point, or anchor-mismatched batch event."""


@dataclass(frozen=True, slots=True)
class BatchPoint:
    point_ref: str
    anchor_ref: str
    coupled_to: str | None = None

    def __post_init__(self) -> None:
        _ref(self.point_ref, "point_ref")
        _ref(self.anchor_ref, "anchor_ref")
        if self.coupled_to is not None:
            _ref(self.coupled_to, "coupled_to")


@dataclass(frozen=True, slots=True)
class BatchEvent:
    event_id: str
    task_id: str
    task_revision: int
    session_id: str
    packet_ref: str
    point_ref: str
    anchor_ref: str
    action: str
    response_ref: str | None = None
    relation_ref: str | None = None

    def __post_init__(self) -> None:
        for value, label in (
            (self.event_id, "event_id"),
            (self.session_id, "session_id"),
            (self.packet_ref, "packet_ref"),
            (self.point_ref, "point_ref"),
            (self.anchor_ref, "anchor_ref"),
        ):
            _ref(value, label)
        if not re.fullmatch(r"AR-\d{4}", self.task_id):
            raise BatchError("task_id is invalid")
        if type(self.task_revision) is not int or self.task_revision < 1:
            raise BatchError("task_revision must be positive")
        if self.action not in {"focus", "respond", "unresolved", "reask", "declare_coupling"}:
            raise BatchError("batch action is invalid")
        if self.response_ref is not None:
            _ref(self.response_ref, "response_ref")
        if self.relation_ref is not None:
            _ref(self.relation_ref, "relation_ref")


@dataclass(slots=True)
class _PointState:
    spec: BatchPoint
    response_ref: str | None = None
    unresolved: bool = True
    reask_count: int = 0
    relation_declared: bool = False


class BatchState:
    """Coordinator-owned packet state with independent per-point outcomes."""

    def __init__(
        self,
        task_id: str,
        task_revision: int,
        session_id: str,
        packet_ref: str,
        points: tuple[BatchPoint, ...],
    ) -> None:
        if not re.fullmatch(r"AR-\d{4}", task_id) or task_revision < 1:
            raise BatchError("batch identity is invalid")
        _ref(session_id, "session_id")
        _ref(packet_ref, "packet_ref")
        if not points or len({point.point_ref for point in points}) != len(points):
            raise BatchError("batch points must be unique and non-empty")
        self.task_id = task_id
        self.task_revision = task_revision
        self.session_id = session_id
        self.packet_ref = packet_ref
        self._points = {point.point_ref: _PointState(point) for point in points}
        self._event_ids: set[str] = set()
        self.active_point: str | None = None

    def apply(self, event: BatchEvent) -> None:  # noqa: C901
        if event.event_id in self._event_ids:
            raise BatchError("duplicate batch event")
        if event.task_id != self.task_id or event.session_id != self.session_id:
            raise BatchError("stale batch session")
        if event.packet_ref != self.packet_ref:
            raise BatchError("batch packet does not match session")
        if event.task_revision != self.task_revision:
            raise BatchError("stale batch event revision")
        point = self._points.get(event.point_ref)
        if point is None:
            raise BatchError("unknown batch point")
        if event.anchor_ref != point.spec.anchor_ref:
            raise BatchError("stale batch anchor")
        if event.action == "focus":
            self.active_point = event.point_ref
        elif event.action == "declare_coupling":
            if point.spec.coupled_to is None or event.relation_ref != point.spec.coupled_to:
                raise BatchError("coupling relation is not declared by the packet")
            for candidate in self._points.values():
                if candidate.spec.coupled_to == point.spec.coupled_to:
                    candidate.relation_declared = True
        elif event.action == "respond":
            if event.response_ref is None:
                raise BatchError("response is required")
            if point.spec.coupled_to is not None and not point.relation_declared:
                raise BatchError("coupling relation must be declared before response")
            point.response_ref = event.response_ref
            point.unresolved = False
        elif event.action == "unresolved":
            point.unresolved = True
        else:
            point.reask_count += 1
            point.response_ref = None
            point.unresolved = True
        self._event_ids.add(event.event_id)
        self.task_revision += 1

    def point(self, point_ref: str) -> tuple[str | None, bool, int]:
        point = self._points.get(point_ref)
        if point is None:
            raise BatchError("unknown batch point")
        return point.response_ref, point.unresolved, point.reask_count


def _ref(value: str, label: str) -> None:
    if not isinstance(value, str) or not PUBLIC_REF.fullmatch(value):
        raise BatchError(f"{label} must be public-safe")
    if value.startswith("/") or ".." in value or "//" in value:
        raise BatchError(f"{label} must be public-safe")
