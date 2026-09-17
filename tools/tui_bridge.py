# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Coordinator-side adapter for the versioned Coordinator/TUI bridge."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


class TuiBridgeError(ValueError):
    """Reject malformed, stale, or cross-task TUI traffic."""


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
