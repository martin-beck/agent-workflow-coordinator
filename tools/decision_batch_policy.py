# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Plan autonomous progress and revision-bound human decision batches.

This is deliberately a pure planning boundary. Coordinator remains the only
authority that mutates AR state; an autonomous worker can use the result to
finish safe work before opening the TUI and to present only the decisions that
actually require the user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


class DecisionBatchError(ValueError):
    """Reject an ambiguous or unsafe autonomous escalation plan."""


@dataclass(frozen=True, slots=True)
class ARDecision:
    """A public-safe snapshot used only to plan the next worker step."""

    ar_id: str
    task_revision: int
    state: str
    requires_human: bool = False
    batch_key: str | None = None
    decision_request_ref: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"AR-\d{4}", self.ar_id):
            raise DecisionBatchError("ar_id is invalid")
        if type(self.task_revision) is not int or self.task_revision < 1:
            raise DecisionBatchError("task_revision must be positive")
        if self.state not in {"ready", "blocked", "uncertain"}:
            raise DecisionBatchError("state is invalid")
        if type(self.requires_human) is not bool:
            raise DecisionBatchError("requires_human must be boolean")
        for value, label in (
            (self.batch_key, "batch_key"),
            (self.decision_request_ref, "decision_request_ref"),
        ):
            if value is not None and not _REF.fullmatch(value):
                raise DecisionBatchError(f"{label} is not public-safe")
        if self.requires_human and not self.decision_request_ref:
            raise DecisionBatchError("human decisions require a request reference")


@dataclass(frozen=True, slots=True)
class HumanBatch:
    """A deterministic group of requests that can be presented together."""

    batch_key: str
    decision_request_refs: tuple[str, ...]
    ar_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProgressPlan:
    """The work an agent may continue and the sessions it must present."""

    autonomous_ar_ids: tuple[str, ...]
    human_batches: tuple[HumanBatch, ...]


def plan_progress(decisions: tuple[ARDecision, ...]) -> ProgressPlan:
    """Finish all safe ARs and batch only explicitly related human requests.

    A missing ``batch_key`` is intentionally never inferred: it creates a
    single-item session.  The returned batch does not authorize any response;
    each request remains independently revision-bound in the TUI/AWG packet.
    """

    seen: set[str] = set()
    autonomous: list[str] = []
    grouped: dict[str, list[ARDecision]] = {}
    for decision in decisions:
        if decision.ar_id in seen:
            raise DecisionBatchError("AR decisions must be unique")
        seen.add(decision.ar_id)
        if decision.state == "ready" and not decision.requires_human:
            autonomous.append(decision.ar_id)
        if decision.requires_human:
            key = decision.batch_key or f"single:{decision.ar_id}"
            grouped.setdefault(key, []).append(decision)

    batches = tuple(
        HumanBatch(
            batch_key=key,
            decision_request_refs=tuple(
                item.decision_request_ref for item in items if item.decision_request_ref
            ),
            ar_ids=tuple(item.ar_id for item in items),
        )
        for key, items in sorted(grouped.items())
    )
    return ProgressPlan(tuple(autonomous), batches)
