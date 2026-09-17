# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

import unittest
from typing import Any

from tools.tui_bridge import TuiBridgeError, apply_tui_response, build_tui_request


def _request() -> tuple[dict[str, Any], dict[str, Any]]:
    ar = {
        "id": "AR-0001",
        "task_revision": 2,
        "status": "open",
        "description": "Plan",
        "specification": {"mode": "x"},
    }
    guidance = {
        "request_id": "AWG-X",
        "context": {"task_ref": "AR-0001", "task_revision": 2},
        "human_interaction": {
            "schema_version": "1.0",
            "interaction_required": True,
            "task_ref": "AR-0001",
            "task_revision": 2,
            "activation": "agent-uncertainty",
            "decision_status": "pending",
            "reason": "uncertain",
            "requested_by": "agent",
            "decision_request_ref": "AWG-X",
            "tui_contract_version": "1.0",
        },
    }
    return ar, build_tui_request(project_id="p", ar=ar, guidance_request=guidance, session_id="s")


class TuiBridgeTests(unittest.TestCase):
    def test_build_and_apply_response(self) -> None:
        ar, req = _request()
        response = {
            "kind": "coordinator-tui-response",
            "project_id": "p",
            "ar_id": "AR-0001",
            "task_revision": 2,
            "decision_request_ref": "AWG-X",
            "event": {"session_id": "s", "sequence": 1, "event_type": "select", "payload": {}},
            "ar_update": {
                "decision_status": "resolved",
                "ar_status": "open",
                "description_append": " -> chosen",
                "specification_update": {
                    "request_id": "AWG-X",
                    "point_id": "AWG-X",
                    "disposition": "selected",
                },
            },
        }
        updated = apply_tui_response(ar=ar, request=req, response=response)
        self.assertEqual(updated["task_revision"], 3)
        self.assertIs(updated["interaction"]["interaction_required"], False)

    def test_trigger_revision_mismatch_fails_closed(self) -> None:
        ar, req = _request()
        req["ar"]["task_revision"] = 9
        with self.assertRaises(TuiBridgeError):
            apply_tui_response(ar=ar, request=req, response={"kind": "coordinator-tui-response"})
