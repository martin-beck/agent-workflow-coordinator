# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Hostile and positive tests for revision-bound discussion sessions."""

import unittest
from typing import Any, cast

from tools.discussion_session import SessionActor, SessionError, SessionEvent, SessionState
from tools.discussion_session_model import ModelState, check_bounded_model, step


def event(revision: int, action: str, event_id: str = "event-1", **kwargs: str) -> SessionEvent:
    if event_id == "event-1":
        event_id = f"event-{revision}-{action}"
    return SessionEvent(
        event_id=event_id,
        task_id="AR-0026",
        task_revision=revision,
        session_id=kwargs.get("session_id", "session-1"),
        packet_ref="awg/packet-1",
        point_ref=kwargs.get("point_ref", "point-1"),
        anchor_ref=kwargs.get("anchor_ref", "plan/anchor-1"),
        action=action,
        actor=SessionActor.USER,
        response_ref="awg/response-1" if action == "respond" else None,
        recorded_at="2026-09-17T00:00:00+00:00",
    )


class DiscussionSessionTests(unittest.TestCase):
    def test_session_event_validation_is_fail_closed(self) -> None:
        valid = {
            "event_id": "event-1",
            "task_id": "AR-0026",
            "task_revision": 1,
            "session_id": "session-1",
            "packet_ref": "awg/packet-1",
            "point_ref": "point-1",
            "anchor_ref": "plan/anchor-1",
            "action": "enter",
            "actor": SessionActor.USER,
            "response_ref": None,
            "recorded_at": "2026-09-17T00:00:00+00:00",
        }
        cases = (
            ("task_id", "bad", "task_id"),
            ("task_revision", 0, "positive"),
            ("action", "nope", "action"),
            ("recorded_at", "not-a-date", "recorded_at"),
            ("recorded_at", "2026-09-17T00:00:00", "timezone"),
            ("packet_ref", "../private", "public-safe"),
        )
        for key, value, message in cases:
            with self.subTest(key=key), self.assertRaisesRegex(SessionError, message):
                cast(Any, SessionEvent)(**{**valid, key: value})

        with self.assertRaisesRegex(SessionError, "public-safe"):
            cast(Any, SessionEvent)(**{**valid, "response_ref": "/private"})

    def test_bounded_model_covers_hostile_and_accepted_session_traces(self) -> None:
        result = check_bounded_model()
        self.assertGreater(result["accepted"], 0)
        self.assertGreater(result["hostile"], 0)
        with self.assertRaisesRegex(ValueError, "wrong active point"):
            step(
                ModelState(2, True, "point-1", "plan/anchor-1", False),
                "respond",
                2,
                "point-2",
                "plan/anchor-1",
            )

    def test_ordered_session_binds_identity_and_keeps_unresolved_explicit(self) -> None:
        state = SessionState("AR-0026", 1)
        state.apply(event(1, "enter"))
        state.apply(event(2, "focus"))
        state.apply(event(3, "unresolved"))
        self.assertTrue(state.unresolved)
        self.assertEqual("session-1", state.session_id)
        self.assertEqual(4, state.task_revision)

    def test_stale_session_wrong_anchor_and_duplicate_are_rejected(self) -> None:
        state = SessionState("AR-0026", 1)
        with self.assertRaisesRegex(SessionError, "skipped entry"):
            state.apply(event(1, "focus"))
        state.apply(event(1, "enter"))
        with self.assertRaisesRegex(SessionError, "stale session identity"):
            state.apply(event(2, "focus", session_id="other-session"))
        with self.assertRaisesRegex(SessionError, "anchor"):
            state.apply(event(2, "focus", anchor_ref="plan/other-anchor"))
        state.apply(event(2, "focus"))
        with self.assertRaisesRegex(SessionError, "duplicate"):
            state.apply(event(2, "focus"))

    def test_stale_revision_and_response_point_are_rejected(self) -> None:
        state = SessionState("AR-0026", 5)
        with self.assertRaisesRegex(SessionError, "stale session event revision"):
            state.apply(event(4, "enter"))
        state.apply(event(5, "enter"))
        state.apply(event(6, "focus", point_ref="point-2"))
        with self.assertRaisesRegex(SessionError, "active point"):
            state.apply(event(7, "respond", point_ref="point-1"))

    def test_session_response_and_unresolved_paths_are_bound(self) -> None:
        state = SessionState("AR-0026", 1)
        state.apply(event(1, "enter"))
        with self.assertRaisesRegex(SessionError, "already recorded"):
            state.apply(event(2, "enter"))
        state.apply(event(2, "respond"))
        self.assertTrue(state.responded)
        self.assertFalse(state.unresolved)
        state.apply(event(3, "unresolved"))
        self.assertTrue(state.unresolved)
        with self.assertRaisesRegex(SessionError, "response event requires"):
            state.apply(
                SessionEvent(
                    "event-4",
                    "AR-0026",
                    4,
                    "session-1",
                    "awg/packet-1",
                    "point-1",
                    "plan/anchor-1",
                    "respond",
                    SessionActor.USER,
                    None,
                    "2026-09-17T00:00:00+00:00",
                )
            )


if __name__ == "__main__":
    unittest.main()
