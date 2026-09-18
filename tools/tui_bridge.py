# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Coordinator-side adapter for the versioned Coordinator/TUI bridge."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from tools.decision_batch_policy import ARDecision, ProgressPlan, plan_progress


class TuiBridgeError(ValueError):
    """Reject malformed, stale, or cross-task TUI traffic."""


class TuiSessionState(StrEnum):
    """Coordinator-owned host/session states for a live TUI handoff."""

    LAUNCH_PENDING = "launch_pending"
    ATTACHED = "attached"
    AWAITING_RESPONSE = "awaiting_response"
    DETACHED = "detached"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class TuiSession:
    """Revision-bound, transport-neutral TUI process/session record."""

    project_id: str
    ar_id: str
    task_revision: int
    request_ref: str
    session_id: str
    state: TuiSessionState = TuiSessionState.LAUNCH_PENDING
    host_ref: str | None = None
    sequence: int = 0

    def __post_init__(self) -> None:
        if not self.project_id or not re.fullmatch(r"AR-\d{4}", self.ar_id):
            raise TuiBridgeError("TUI session identity is invalid")
        if self.task_revision < 1 or not re.fullmatch(r"AWG-[A-Z0-9-]+", self.request_ref):
            raise TuiBridgeError("TUI session request binding is invalid")
        if not re.fullmatch(r"AWTUI-[A-Z0-9-]+", self.session_id):
            raise TuiBridgeError("TUI session ID is invalid")
        if self.sequence < 0:
            raise TuiBridgeError("TUI session sequence is invalid")

    def attach(self, host_ref: str) -> TuiSession:
        if self.state not in {TuiSessionState.LAUNCH_PENDING, TuiSessionState.DETACHED}:
            raise TuiBridgeError(f"cannot attach TUI session from {self.state}")
        if not host_ref or "/" in host_ref or ".." in host_ref:
            raise TuiBridgeError("TUI host reference is invalid")
        return replace(self, state=TuiSessionState.ATTACHED, host_ref=host_ref)

    def await_response(self) -> TuiSession:
        if self.state != TuiSessionState.ATTACHED:
            raise TuiBridgeError(f"cannot await response from {self.state}")
        return replace(self, state=TuiSessionState.AWAITING_RESPONSE)

    def detach(self) -> TuiSession:
        if self.state not in {TuiSessionState.ATTACHED, TuiSessionState.AWAITING_RESPONSE}:
            raise TuiBridgeError(f"cannot detach TUI session from {self.state}")
        return replace(self, state=TuiSessionState.DETACHED)

    def resolved(self, sequence: int) -> TuiSession:
        if self.state != TuiSessionState.AWAITING_RESPONSE:
            raise TuiBridgeError(f"cannot resolve TUI session from {self.state}")
        if sequence <= self.sequence:
            raise TuiBridgeError("TUI response sequence is stale")
        return replace(self, state=TuiSessionState.RESOLVED, sequence=sequence)


def prepare_tui_session(
    *, project_id: str, ar: dict[str, Any], guidance_request: dict[str, Any], session_id: str
) -> tuple[TuiSession, dict[str, Any]]:
    """Create the launch-pending record and its exact request envelope."""

    request = build_tui_request(
        project_id=project_id, ar=ar, guidance_request=guidance_request, session_id=session_id
    )
    return (
        TuiSession(
            project_id=project_id,
            ar_id=ar["id"],
            task_revision=ar["task_revision"],
            request_ref=request["interaction"]["decision_request_ref"],
            session_id=session_id,
        ),
        request,
    )


def plan_tui_escalation(decisions: tuple[ARDecision, ...]) -> ProgressPlan:
    """Expose the Coordinator-owned autonomous-before-human planning boundary."""

    return plan_progress(decisions)


def build_tui_request(
    *, project_id: str, ar: dict[str, Any], guidance_request: dict[str, Any], session_id: str
) -> dict[str, Any]:
    interaction = guidance_request.get("human_interaction")
    if not isinstance(interaction, dict) or interaction.get("interaction_required") is not True:
        raise TuiBridgeError("Guidance request has no explicit human interaction trigger")
    if interaction.get("task_ref") != ar.get("id") or interaction.get("task_revision") != ar.get(
        "task_revision"
    ):
        raise TuiBridgeError("trigger does not match AR revision")
    return {
        "schema_version": "1.0",
        "kind": "coordinator-tui-request",
        "project_id": project_id,
        "session_id": session_id,
        "ar": {
            "ar_id": ar["id"],
            "task_revision": ar["task_revision"],
            "status": ar.get("status", "open"),
            "description": ar.get("description", ""),
            "specification": ar.get("specification", {}),
        },
        "interaction": interaction,
        "guidance_request": guidance_request,
    }


def apply_tui_response(
    *, ar: dict[str, Any], request: dict[str, Any], response: dict[str, Any]
) -> dict[str, Any]:
    if response.get("kind") != "coordinator-tui-response" or response.get(
        "project_id"
    ) != request.get("project_id"):
        raise TuiBridgeError("response is not for this Coordinator request")
    if response.get("ar_id") != ar.get("id") or response.get("task_revision") != ar.get(
        "task_revision"
    ):
        raise TuiBridgeError("response AR revision is stale")
    if response.get("decision_request_ref") != request["interaction"]["decision_request_ref"]:
        raise TuiBridgeError("response request reference does not match")
    event = response.get("event", {})
    if event.get("session_id") != request.get("session_id"):
        raise TuiBridgeError("response session does not match")
    update = response.get("ar_update")
    if not isinstance(update, dict):
        raise TuiBridgeError("response has no AR persistence update")
    result = deepcopy(ar)
    result["status"] = update["ar_status"]
    result.setdefault("history", []).append(
        {"kind": "tui-response", "event": event, "description_append": update["description_append"]}
    )
    result["description"] = result.get("description", "") + update["description_append"]
    result["specification"] = {**result.get("specification", {}), **update["specification_update"]}
    result["task_revision"] += 1
    result["interaction"] = {
        "decision_status": update["decision_status"],
        "interaction_required": update["decision_status"] not in {"resolved", "superseded"},
        "decision_request_ref": request["interaction"]["decision_request_ref"],
    }
    return result
