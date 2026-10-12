# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Small bounded safety/liveness model for local issue-series creation.

This executable model is deliberately a best-effort abstraction, not a proof
that the implementation refines it.  It checks the whole-series commit
boundary independently of transport, filesystem, and SQLite details.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelState:
    """One two-creator, two-identity bounded creation state."""

    committed: frozenset[str]
    claimed_ids: frozenset[str]
    phase: tuple[str, str]


IDENTITIES = ("issue-a", "issue-b")
CREATORS = ("creator-a", "creator-b")
MEMBERS = {
    "issue-a": frozenset({"AR-9000", "AR-9001"}),
    "issue-b": frozenset({"AR-9002", "AR-9003"}),
}


def _replace_phase(state: ModelState, creator: int, value: str) -> tuple[str, str]:
    return (value, state.phase[1]) if creator == 0 else (state.phase[0], value)


def step(state: ModelState, creator: int, identity: str, action: str) -> ModelState:
    """Take one all-or-nothing create attempt, or reject an invalid transition."""
    if creator not in range(len(CREATORS)) or identity not in IDENTITIES:
        raise ValueError("unknown creator or issue identity")
    if action not in {"prepare", "commit", "rollback", "stale"}:
        raise ValueError("unknown issue-series action")
    phase = state.phase[creator]
    if action == "prepare":
        if phase != "absent" or identity in state.committed:
            raise ValueError("duplicate issue-series prepare")
        return ModelState(
            state.committed, state.claimed_ids, _replace_phase(state, creator, identity)
        )
    if phase != identity:
        raise ValueError("creator has no matching prepared series")
    if action == "rollback":
        return ModelState(
            state.committed, state.claimed_ids, _replace_phase(state, creator, "absent")
        )
    if action == "stale":
        raise ValueError("stale receipt is rejected")
    members = MEMBERS[identity]
    if identity in state.committed or members & state.claimed_ids:
        raise ValueError("duplicate issue identity or AR identifier")
    return ModelState(
        state.committed | {identity},
        state.claimed_ids | members,
        _replace_phase(state, creator, "committed"),
    )


def _invariant(state: ModelState) -> None:
    expected_ids = frozenset().union(*(MEMBERS[item] for item in state.committed))
    if state.claimed_ids != expected_ids:
        raise AssertionError("partial issue-series members became authoritative")


def check_bounded_model() -> dict[str, int]:
    """Explore bounded hostile interleavings and prove an uncontended commit exists."""
    initial = ModelState(frozenset(), frozenset(), ("absent", "absent"))
    states = {initial}
    accepted = rejected = 0
    for _depth in range(5):
        next_states: set[ModelState] = set()
        for state in states:
            _invariant(state)
            for creator in range(len(CREATORS)):
                for identity in IDENTITIES:
                    for action in ("prepare", "commit", "rollback", "stale"):
                        try:
                            candidate = step(state, creator, identity, action)
                        except ValueError:
                            rejected += 1
                        else:
                            _invariant(candidate)
                            accepted += 1
                            next_states.add(candidate)
        states = next_states
    uncontended = step(step(initial, 0, "issue-a", "prepare"), 0, "issue-a", "commit")
    _invariant(uncontended)
    if "issue-a" not in uncontended.committed or not accepted or not rejected:
        raise AssertionError("bounded model lacks liveness or hostile coverage")
    return {"states": len(states), "accepted": accepted, "rejected": rejected}
