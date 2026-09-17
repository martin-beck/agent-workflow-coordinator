# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Finite safety model for revision-bound discussion-session events."""

from __future__ import annotations

from dataclasses import dataclass

ACTIONS = ("enter", "focus", "respond", "unresolved")


@dataclass(frozen=True, slots=True)
class ModelState:
    revision: int
    entered: bool
    active_point: str | None
    anchor: str | None
    unresolved: bool


def step(state: ModelState, action: str, revision: int, point: str, anchor: str) -> ModelState:
    """Return a state or reject a stale, skipped, or mismatched transition."""
    if action not in ACTIONS:
        raise ValueError("unknown session action")
    if revision != state.revision:
        raise ValueError("stale session event revision")
    if state.anchor is not None and anchor != state.anchor:
        raise ValueError("wrong session anchor")
    if action == "enter":
        if state.entered:
            raise ValueError("duplicate session entry")
        return ModelState(revision + 1, True, point, anchor, False)
    if not state.entered:
        raise ValueError("skipped session entry")
    if action == "focus":
        return ModelState(revision + 1, True, point, state.anchor, state.unresolved)
    if point != state.active_point:
        raise ValueError("wrong active point")
    return ModelState(revision + 1, True, state.active_point, state.anchor, action == "unresolved")


def check_bounded_model() -> dict[str, int]:
    """Explore short traces and require both accepted and hostile outcomes."""
    states = {ModelState(1, False, None, None, False)}
    accepted = hostile = 0
    for _ in range(4):
        next_states: set[ModelState] = set()
        for state in states:
            for action in (*ACTIONS, "unknown"):
                for point, anchor, expected in (
                    ("point-1", "plan/anchor-1", state.revision),
                    ("point-2", "plan/other", state.revision),
                    ("point-1", "plan/anchor-1", state.revision - 1),
                ):
                    try:
                        candidate = step(state, action, expected, point, anchor)
                    except ValueError:
                        hostile += 1
                    else:
                        accepted += 1
                        next_states.add(candidate)
        states = next_states
    if not accepted or not hostile:
        raise AssertionError("bounded model lacks accepted or hostile traces")
    return {"states": len(states), "accepted": accepted, "hostile": hostile}
