# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

import unittest
from typing import Any

from tools.decision_batch_policy import ARDecision
from tools.tui_bridge import (
    TuiBridgeError,
    TuiSession,
    TuiSessionState,
    apply_tui_response,
    build_tui_request,
    plan_tui_escalation,
    prepare_tui_session,
)


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
    def test_session_launch_attach_await_detach_and_resume(self) -> None:
        ar, request = _request()
        request["session_id"] = "AWTUI-SESSION-1"
        session, _ = prepare_tui_session(
            project_id="p",
            ar=ar,
            guidance_request={
                "human_interaction": {
                    **request["interaction"],
                    "decision_request_ref": "AWG-X",
                },
                "request_id": "AWG-X",
            },
            session_id="AWTUI-SESSION-1",
        )
        self.assertEqual(TuiSessionState.LAUNCH_PENDING, session.state)
        session = session.attach("host-1").await_response()
        self.assertEqual(TuiSessionState.AWAITING_RESPONSE, session.state)
        self.assertEqual(TuiSessionState.ATTACHED, session.detach().attach("host-2").state)
        self.assertEqual(TuiSessionState.RESOLVED, session.resolved(1).state)

    def test_session_rejects_invalid_transition_or_stale_response(self) -> None:
        session = TuiSession("p", "AR-0001", 2, "AWG-X", "AWTUI-S-1")
        with self.assertRaises(TuiBridgeError):
            session.await_response()
        with self.assertRaises(TuiBridgeError):
            session.attach("../host")
        with self.assertRaises(TuiBridgeError):
            session.attach("")
        attached = session.attach("host").await_response()
        with self.assertRaises(TuiBridgeError):
            attached.await_response()
        with self.assertRaises(TuiBridgeError):
            attached.resolved(1)
        with self.assertRaises(TuiBridgeError):
            attached.detach().detach()
        with self.assertRaises(TuiBridgeError):
            attached.resolved(0)

    def test_session_rejects_invalid_identity(self) -> None:
        for values in (
            {
                "project_id": "",
                "ar_id": "AR-0001",
                "request_ref": "AWG-X",
                "session_id": "AWTUI-S-1",
            },
            {"project_id": "p", "ar_id": "bad", "request_ref": "AWG-X", "session_id": "AWTUI-S-1"},
            {
                "project_id": "p",
                "ar_id": "AR-0001",
                "request_ref": "bad",
                "session_id": "AWTUI-S-1",
            },
            {"project_id": "p", "ar_id": "AR-0001", "request_ref": "AWG-X", "session_id": "bad"},
            {
                "project_id": "p",
                "ar_id": "AR-0001",
                "request_ref": "AWG-X",
                "session_id": "AWTUI-S-1",
                "sequence": -1,
            },
        ):
            with self.subTest(values=values), self.assertRaises(TuiBridgeError):
                TuiSession(task_revision=1, **values)

    def test_escalation_plan_finishes_ready_work_first(self) -> None:
        plan = plan_tui_escalation(
            (
                ARDecision("AR-0001", 1, "ready"),
                ARDecision("AR-0002", 1, "uncertain", True, "batch-1", "AWG-AR-0002"),
            )
        )
        self.assertEqual(plan.autonomous_ar_ids, ("AR-0001",))
        self.assertEqual(plan.human_batches[0].ar_ids, ("AR-0002",))

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

    def test_request_rejects_missing_or_stale_guidance_trigger(self) -> None:
        ar, _req = _request()
        for guidance in (
            {},
            {"human_interaction": {"interaction_required": False}},
            {
                "human_interaction": {
                    "interaction_required": True,
                    "task_ref": "AR-9999",
                    "task_revision": 2,
                }
            },
        ):
            with self.subTest(guidance=guidance), self.assertRaises(TuiBridgeError):
                build_tui_request(project_id="p", ar=ar, guidance_request=guidance, session_id="s")

    def test_response_rejects_each_binding_mismatch(self) -> None:
        ar, req = _request()
        base = {
            "kind": "coordinator-tui-response",
            "project_id": "p",
            "ar_id": "AR-0001",
            "task_revision": 2,
            "decision_request_ref": "AWG-X",
            "event": {"session_id": "s"},
            "ar_update": {},
        }
        cases: tuple[tuple[str, object, str], ...] = (
            ("kind", "wrong", "Coordinator request"),
            ("project_id", "other", "Coordinator request"),
            ("ar_id", "AR-0002", "AR revision"),
            ("task_revision", 3, "AR revision"),
            ("decision_request_ref", "AWG-Y", "request reference"),
            ("event", {"session_id": "other"}, "session"),
            ("event", {}, "session"),
            ("ar_update", None, "persistence update"),
        )
        for key, value, message in cases:
            with self.subTest(key=key, value=value):
                response = {**base, key: value}
                with self.assertRaisesRegex(TuiBridgeError, message):
                    apply_tui_response(ar=ar, request=req, response=response)
