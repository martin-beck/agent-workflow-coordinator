# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Coordinator-side adapter for the versioned Coordinator/TUI bridge."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from tools.decision_batch_policy import ARDecision, ProgressPlan, plan_progress
from tools.decision_routing import DecisionAssessment, require_tui_route


class TuiBridgeError(ValueError):
    """Reject malformed, stale, or cross-task TUI traffic."""


def enforce_decision_route(
    assessment: DecisionAssessment,
    *,
    trigger: dict[str, Any] | None,
    request_ref: str | None,
    channel: str,
    host_direct_question: bool = False,
) -> None:
    """Apply the mandatory routing gate at the Coordinator/TUI boundary."""

    try:
        require_tui_route(
            assessment,
            trigger=trigger,
            request_ref=request_ref,
            channel=channel,
            host_direct_question=host_direct_question,
        )
    except ValueError as error:
        raise TuiBridgeError(str(error)) from error


MAX_TUI_EVENT_JOURNAL_BYTES = 2 * 1024 * 1024


def _validate_journal_event(
    event: object, *, expected: tuple[object, ...], sequence: int
) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise TuiBridgeError("TUI event journal entry is not an object")
    identity = (
        event.get("project_id"),
        event.get("ar_id"),
        event.get("task_revision"),
        event.get("session_id"),
    )
    if identity != expected:
        raise TuiBridgeError("TUI event journal identity does not match request")
    if not isinstance(event.get("sequence"), int) or event["sequence"] != sequence + 1:
        raise TuiBridgeError("TUI event journal sequence is not contiguous")
    return event


def read_tui_event_journal(
    path: str | Path, *, request: dict[str, Any]
) -> tuple[dict[str, Any], ...]:
    """Read and validate a private TUI event journal without mutating AR state.

    The Coordinator caller may then apply an explicitly selected event through
    ``apply_tui_response``. This separation prevents a disconnected or replayed
    journal from silently authorizing multiple AR revisions.
    """
    candidate = Path(path)
    try:
        raw = candidate.read_bytes()
    except OSError as error:
        raise TuiBridgeError("TUI event journal is unavailable") from error
    if len(raw) > MAX_TUI_EVENT_JOURNAL_BYTES:
        raise TuiBridgeError("TUI event journal exceeds bounded size")
    if not candidate.is_file():
        raise TuiBridgeError("TUI event journal is not a regular file")
    if candidate.stat().st_mode & 0o077:
        raise TuiBridgeError("TUI event journal must be private")
    expected = (
        request.get("project_id"),
        request.get("ar", {}).get("ar_id"),
        request.get("ar", {}).get("task_revision"),
        request.get("session_id"),
    )
    events: list[dict[str, Any]] = []
    sequence = 0
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise TuiBridgeError("TUI event journal contains invalid JSON") from error
        event = _validate_journal_event(event, expected=expected, sequence=sequence)
        sequence = event["sequence"]
        events.append(event)
    return tuple(events)


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
    *,
    project_id: str,
    ar: dict[str, Any],
    guidance_request: dict[str, Any],
    session_id: str,
    documents: dict[str, str] | None = None,
) -> tuple[TuiSession, dict[str, Any]]:
    """Create the launch-pending record and its exact request envelope."""

    request = build_tui_request(
        project_id=project_id,
        ar=ar,
        guidance_request=guidance_request,
        session_id=session_id,
        documents=documents,
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


def generate_tui_documents(
    ars: tuple[dict[str, Any], ...], *, decision_requests: tuple[dict[str, Any], ...] = ()
) -> dict[str, str]:
    """Generate the authoritative Markdown context from the complete AR graph.

    The output is deterministic and includes every AR exactly once.  Decision
    objectives are included verbatim as anchorable phrases so the TUI can
    highlight the same source text in both documents.
    """
    if not ars:
        raise TuiBridgeError("cannot generate TUI documents without ARs")
    by_id = {ar.get("id"): ar for ar in ars}
    if len(by_id) != len(ars) or any(not isinstance(key, str) for key in by_id):
        raise TuiBridgeError("AR graph contains duplicate or invalid IDs")
    for ar in ars:
        dependencies = ar.get("depends_on", ())
        if not isinstance(dependencies, (list, tuple)) or any(
            dep not in by_id for dep in dependencies
        ):
            raise TuiBridgeError(f"AR {ar.get('id')} has an unknown dependency")
    ordered = sorted(ars, key=lambda ar: str(ar["id"]))
    request_by_task = {
        request.get("context", {}).get("task_ref"): request for request in decision_requests
    }
    design = ["# Design", "", "## AR decision context"]
    workplan = ["# Work plan", "", "## Dependency-ordered AR work"]
    for ar in ordered:
        ar_id = ar["id"]
        summary = str(ar.get("summary") or ar.get("title") or ar_id)
        description = str(ar.get("description") or summary)
        request = request_by_task.get(ar_id)
        objective = request.get("context", {}).get("objective") if request else None
        phrase = str(objective or summary)
        design.extend([f"### {ar_id}: {summary}", description, f"Decision focus: {phrase}", ""])
        dependencies = ", ".join(str(dep) for dep in ar.get("depends_on", ())) or "none"
        workplan.extend([f"### {ar_id}", f"Depends on: {dependencies}", f"Work item: {phrase}", ""])
    return {
        "design": "\n".join(design).rstrip() + "\n",
        "workplan": "\n".join(workplan).rstrip() + "\n",
    }


def prepare_tui_batch_session(
    *,
    project_id: str,
    entries: tuple[tuple[dict[str, Any], dict[str, Any]], ...],
    session_id: str,
    ars: tuple[dict[str, Any], ...] | None = None,
) -> tuple[TuiSession, dict[str, Any]]:
    """Create one TUI session for all independent decisions in a batch."""
    if not entries:
        raise TuiBridgeError("TUI batch cannot be empty")
    documents = generate_tui_documents(
        ars or tuple(ar for ar, _request in entries),
        decision_requests=tuple(request for _ar, request in entries),
    )
    first_ar, first_request = entries[0]
    request = build_tui_request(
        project_id=project_id,
        ar=first_ar,
        guidance_request=first_request,
        session_id=session_id,
        documents=documents,
    )
    request["batch"] = [
        build_tui_request(
            project_id=project_id,
            ar=ar,
            guidance_request=guidance,
            session_id=session_id,
            documents=documents,
        )
        | {"documents": documents}
        for ar, guidance in entries
    ]
    return (
        TuiSession(
            project_id=project_id,
            ar_id=first_ar["id"],
            task_revision=first_ar["task_revision"],
            request_ref=request["interaction"]["decision_request_ref"],
            session_id=session_id,
        ),
        request,
    )


def plan_tui_escalation(decisions: tuple[ARDecision, ...]) -> ProgressPlan:
    """Expose the Coordinator-owned autonomous-before-human planning boundary."""

    return plan_progress(decisions)


def build_tui_request(
    *,
    project_id: str,
    ar: dict[str, Any],
    guidance_request: dict[str, Any],
    session_id: str,
    documents: dict[str, str] | None = None,
) -> dict[str, Any]:
    interaction = guidance_request.get("human_interaction")
    if not isinstance(interaction, dict) or interaction.get("interaction_required") is not True:
        raise TuiBridgeError("Guidance request has no explicit human interaction trigger")
    if interaction.get("task_ref") != ar.get("id") or interaction.get("task_revision") != ar.get(
        "task_revision"
    ):
        raise TuiBridgeError("trigger does not match AR revision")
    request = {
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
    if documents is not None:
        if set(documents) - {"design", "workplan"} or not all(
            isinstance(value, str) for value in documents.values()
        ):
            raise TuiBridgeError("TUI documents must be design/workplan Markdown strings")
        request["documents"] = dict(documents)
    return request


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
