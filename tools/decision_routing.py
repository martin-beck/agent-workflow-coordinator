# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Fail-closed assessment and routing for agent decisions.

The host-agent conversation can announce a pending TUI session, but it is
never an authority channel.  This module is intentionally pure so adapters
for Codex, OpenCode, and other hosts can share the same gate.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


class DecisionRoutingError(ValueError):
    """Raised when an important decision bypasses the AR--TUI bridge."""


UI_CHANNEL = "workflow-ui"


@dataclass(frozen=True, slots=True)
class DecisionAssessment:
    """Public-safe facts used to decide whether human authority is required."""

    decision_class: str
    impact: str = "low"
    reversibility: str = "easy"
    uncertainty: bool = False
    user_requested: bool = False
    proposal_review: bool = False
    policy_required: bool = False
    project_direction: bool = False

    @property
    def reason_codes(self) -> tuple[str, ...]:
        candidates = (
            ("user-request", self.user_requested),
            ("proposal-review", self.proposal_review),
            ("agent-uncertainty", self.uncertainty),
            ("policy-required", self.policy_required),
            ("design-choice", self.decision_class == "design"),
            ("conceptual-choice", self.decision_class == "conceptual"),
            ("high-impact", self.impact in {"high", "critical"}),
            ("irreversible", self.reversibility in {"difficult", "irreversible"}),
            ("project-direction", self.project_direction),
        )
        return tuple(code for code, enabled in candidates if enabled)

    @property
    def requires_tui(self) -> bool:
        return bool(
            self.reason_codes
            and (
                self.user_requested
                or self.proposal_review
                or self.uncertainty
                or self.policy_required
                or self.decision_class in {"design", "conceptual"}
                or self.impact in {"high", "critical"}
                or self.reversibility in {"difficult", "irreversible"}
                or self.project_direction
            )
        )


def assess_decision(**kwargs: object) -> DecisionAssessment:
    """Classify a proposed action before an agent asks for user authority."""

    assessment = DecisionAssessment(**kwargs)  # type: ignore[arg-type]
    if assessment.decision_class not in {"design", "conceptual", "operational"}:
        raise DecisionRoutingError("decision_class is invalid")
    if assessment.impact not in {"low", "medium", "high", "critical"}:
        raise DecisionRoutingError("impact is invalid")
    if assessment.reversibility not in {"easy", "bounded", "difficult", "irreversible"}:
        raise DecisionRoutingError("reversibility is invalid")
    return assessment


def require_tui_route(
    assessment: DecisionAssessment,
    *,
    trigger: dict[str, object] | None,
    request_ref: str | None,
    channel: str,
    host_direct_question: bool = False,
) -> None:
    """Reject direct host questions and incomplete bridge handoffs."""

    if not assessment.requires_tui:
        return
    if host_direct_question or channel != UI_CHANNEL:
        raise DecisionRoutingError(
            "important decisions must use workflow-ui (GUI preferred, TUI fallback)"
        )
    if not request_ref or not isinstance(request_ref, str) or not request_ref.startswith("AWG-"):
        raise DecisionRoutingError("important decisions require an AWG request reference")
    if not isinstance(trigger, dict) or trigger.get("interaction_required") is not True:
        raise DecisionRoutingError("important decisions require an active human trigger")
    if trigger.get("decision_request_ref") != request_ref:
        raise DecisionRoutingError("trigger and TUI request references must match")


def require_all_tui_routes(
    assessments: Iterable[tuple[DecisionAssessment, dict[str, object] | None, str | None]],
) -> None:
    """Validate a batch without allowing one item to bypass the bridge."""

    for assessment, trigger, request_ref in assessments:
        require_tui_route(
            assessment,
            trigger=trigger,
            request_ref=request_ref,
            channel=UI_CHANNEL,
        )
