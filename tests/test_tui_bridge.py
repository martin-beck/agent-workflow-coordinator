# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from tools.decision_batch_policy import ARDecision
from tools.decision_routing import assess_decision
from tools.tui_bridge import (
    MAX_TUI_EVENT_JOURNAL_BYTES,
    TuiBridgeError,
    TuiSession,
    TuiSessionState,
    apply_tui_response,
    build_tui_request,
    enforce_decision_route,
    generate_tui_documents,
    plan_tui_escalation,
    prepare_tui_batch_session,
    prepare_tui_session,
    read_tui_event_journal,
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
    def test_complete_ar_graph_generates_both_documents_and_matching_focus(self) -> None:
        ars = (
            {
                "id": "AR-0001",
                "summary": "Boundary",
                "description": "Choose boundary",
                "depends_on": [],
            },
            {
                "id": "AR-0002",
                "summary": "Rollout",
                "description": "Choose rollout",
                "depends_on": ["AR-0001"],
            },
        )
        request = {"context": {"task_ref": "AR-0001", "objective": "Choose boundary"}}
        docs = generate_tui_documents(ars, decision_requests=(request,))
        self.assertIn("Choose boundary", docs["design"])
        self.assertIn("Choose boundary", docs["workplan"])
        self.assertTrue(docs["design"].startswith("# Design\n"))
        self.assertTrue(docs["workplan"].startswith("# Work plan\n"))
        with self.assertRaisesRegex(TuiBridgeError, "without ARs"):
            generate_tui_documents(())
        with self.assertRaisesRegex(TuiBridgeError, "unknown dependency"):
            generate_tui_documents(({"id": "AR-0001", "depends_on": ["AR-9999"]},))

    def test_document_generation_rejects_dependency_cycles(self) -> None:
        with self.assertRaisesRegex(TuiBridgeError, "dependency cycle"):
            generate_tui_documents(
                (
                    {"id": "AR-0001", "depends_on": ["AR-0002"]},
                    {"id": "AR-0002", "depends_on": ["AR-0001"]},
                )
            )

    def test_batch_session_contains_all_requests_in_one_launch(self) -> None:
        first_ar, first_wrapper = _request()
        first = first_wrapper["guidance_request"]
        second_ar = {**first_ar, "id": "AR-0002", "task_revision": 1}
        second = {
            **first,
            "request_id": "AWG-Y",
            "context": {"task_ref": "AR-0002", "task_revision": 1},
            "human_interaction": {
                **first["human_interaction"],
                "task_ref": "AR-0002",
                "task_revision": 1,
                "decision_request_ref": "AWG-Y",
            },
        }
        session, request = prepare_tui_batch_session(
            project_id="p",
            entries=((first_ar, first), (second_ar, second)),
            session_id="AWTUI-BATCH-1",
        )
        self.assertEqual(session.ar_id, "AR-0001")
        self.assertEqual(len(request["batch"]), 2)
        self.assertEqual(
            {item["guidance_request"]["request_id"] for item in request["batch"]},
            {"AWG-X", "AWG-Y"},
        )

    def test_session_rejects_invalid_identity_and_host(self) -> None:
        for args in (
            ("", "AR-0001", 1, "AWG-X", "AWTUI-S-1"),
            ("p", "bad", 1, "AWG-X", "AWTUI-S-1"),
            ("p", "AR-0001", 0, "AWG-X", "AWTUI-S-1"),
            ("p", "AR-0001", 1, "bad", "AWTUI-S-1"),
            ("p", "AR-0001", 1, "AWG-X", "bad"),
        ):
            with self.subTest(args=args), self.assertRaises(TuiBridgeError):
                TuiSession(*args)
        session = TuiSession("p", "AR-0001", 1, "AWG-X", "AWTUI-S-1")
        for host in ("", "../host", "host/child"):
            with self.subTest(host=host), self.assertRaises(TuiBridgeError):
                session.attach(host)

    def test_session_rejects_invalid_state_transitions_and_stale_sequence(self) -> None:
        session = TuiSession("p", "AR-0001", 1, "AWG-X", "AWTUI-S-1")
        attached = session.attach("host")
        with self.assertRaises(TuiBridgeError):
            attached.attach("other")
        with self.assertRaises(TuiBridgeError):
            session.detach()
        awaiting = attached.await_response()
        with self.assertRaises(TuiBridgeError):
            awaiting.await_response()
        with self.assertRaises(TuiBridgeError):
            awaiting.resolved(0)
        resolved = awaiting.resolved(1)
        with self.assertRaises(TuiBridgeError):
            resolved.detach()

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
            session.resolved(1)
        with self.assertRaises(TuiBridgeError):
            session.attach("../host")
        with self.assertRaises(TuiBridgeError):
            session.attach("")
        attached = session.attach("host").await_response()
        with self.assertRaises(TuiBridgeError):
            attached.await_response()
        with self.assertRaises(TuiBridgeError):
            attached.detach().detach()
        with self.assertRaises(TuiBridgeError):
            attached.resolved(0)

    def test_event_journal_reader_rejects_unavailable_unsafe_and_malformed_input(self) -> None:
        _ar, request = _request()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing.jsonl"
            with self.assertRaisesRegex(TuiBridgeError, "unavailable"):
                read_tui_event_journal(missing, request=request)

            oversized = root / "oversized.jsonl"
            oversized.write_bytes(b"x" * (MAX_TUI_EVENT_JOURNAL_BYTES + 1))
            oversized.chmod(0o600)
            with self.assertRaisesRegex(TuiBridgeError, "bounded size"):
                read_tui_event_journal(oversized, request=request)

            journal_dir = root / "journal-dir"
            journal_dir.mkdir()
            with self.assertRaisesRegex(TuiBridgeError, "unavailable"):
                read_tui_event_journal(journal_dir, request=request)

            private = root / "private.jsonl"
            private.write_text("{}\n")
            private.chmod(0o644)
            with self.assertRaisesRegex(TuiBridgeError, "private"):
                read_tui_event_journal(private, request=request)

            malformed = root / "malformed.jsonl"
            malformed.write_text("not-json\n")
            malformed.chmod(0o600)
            with self.assertRaisesRegex(TuiBridgeError, "invalid JSON"):
                read_tui_event_journal(malformed, request=request)

            non_object = root / "non-object.jsonl"
            non_object.write_text("[]\n")
            non_object.chmod(0o600)
            with self.assertRaisesRegex(TuiBridgeError, "not an object"):
                read_tui_event_journal(non_object, request=request)

            foreign = root / "foreign.jsonl"
            foreign.write_text(
                json.dumps(
                    {
                        "project_id": "other",
                        "ar_id": "AR-0001",
                        "task_revision": 2,
                        "session_id": "s",
                        "sequence": 1,
                    }
                )
                + "\n"
            )
            foreign.chmod(0o600)
            with self.assertRaisesRegex(TuiBridgeError, "does not match"):
                read_tui_event_journal(foreign, request=request)

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
        pending_response = {
            **response,
            "ar_update": dict(cast(dict[str, Any], response["ar_update"])),
        }
        cast(dict[str, Any], pending_response["ar_update"])["decision_status"] = "pending"
        pending = apply_tui_response(ar=ar, request=req, response=pending_response)
        self.assertIs(pending["interaction"]["interaction_required"], True)

    def test_prepare_session_preserves_markdown_documents(self) -> None:
        ar, _ = _request()
        guidance = {
            "human_interaction": {
                **_request()[1]["interaction"],
                "task_ref": "AR-0001",
                "task_revision": 2,
            },
            "request_id": "AWG-X",
        }
        _session, request = prepare_tui_session(
            project_id="p",
            ar=ar,
            guidance_request=guidance,
            session_id="AWTUI-S-1",
            documents={"design": "# Design\n\nBoundary", "workplan": "# Work plan\n\nValidate"},
        )
        self.assertEqual("# Design\n\nBoundary", request["documents"]["design"])
        self.assertEqual("# Work plan\n\nValidate", request["documents"]["workplan"])

    def test_prepare_session_carries_explicit_windows_ssh_handoff(self) -> None:
        ar, _ = _request()
        guidance = {
            "human_interaction": {
                **_request()[1]["interaction"],
                "task_ref": "AR-0001",
                "task_revision": 2,
            },
            "request_id": "AWG-X",
        }
        handoff = {
            "ssh_host": "project-prod",
            "remote_session_file": "/srv/state/request.json",
            "remote_event_file": "/srv/state/response.json",
            "client_capabilities": {"platform": "windows", "shell": "powershell"},
        }
        _session, request = prepare_tui_session(
            project_id="p", ar=ar, guidance_request=guidance,
            session_id="AWTUI-S-1", host_handoff=handoff,
        )
        self.assertEqual(handoff, request["host_handoff"])

    def test_prepare_session_rejects_private_or_unknown_document_fields(self) -> None:
        ar, request = _request()
        guidance = {
            "human_interaction": {
                **request["interaction"],
                "task_ref": "AR-0001",
                "task_revision": 2,
            },
            "request_id": "AWG-X",
        }
        with self.assertRaisesRegex(TuiBridgeError, "documents"):
            prepare_tui_session(
                project_id="p",
                ar=ar,
                guidance_request=guidance,
                session_id="AWTUI-S-1",
                documents={"design": "x", "private_path": "/secret"},
            )

    def test_event_journal_reader_validates_identity_and_sequence(self) -> None:
        _ar, request = _request()
        events = [
            {
                "project_id": "p",
                "ar_id": "AR-0001",
                "task_revision": 2,
                "session_id": "s",
                "sequence": 1,
                "event_type": "select",
            },
            {
                "project_id": "p",
                "ar_id": "AR-0001",
                "task_revision": 2,
                "session_id": "s",
                "sequence": 2,
                "event_type": "safe-exit",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.events.jsonl"
            with path.open("w", encoding="utf-8") as stream:
                for event in events:
                    stream.write(json.dumps(event) + "\n")
            path.chmod(0o600)
            self.assertEqual(events, list(read_tui_event_journal(path, request=request)))
            events[1]["sequence"] = 4
            with path.open("w", encoding="utf-8") as stream:
                for event in events:
                    stream.write(json.dumps(event) + "\n")
            with self.assertRaisesRegex(TuiBridgeError, "contiguous"):
                read_tui_event_journal(path, request=request)

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

    def test_bridge_rejects_direct_host_question_for_important_decision(self) -> None:
        assessment = assess_decision(decision_class="design")
        with self.assertRaisesRegex(TuiBridgeError, "workflow-ui"):
            enforce_decision_route(
                assessment,
                trigger={"interaction_required": True, "decision_request_ref": "AWG-X"},
                request_ref="AWG-X",
                channel="codex-host",
                host_direct_question=True,
            )
