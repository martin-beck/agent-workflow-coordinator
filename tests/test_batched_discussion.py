# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Focused batch-point, coupling, stale-anchor, and re-ask tests."""

import unittest

from tools.batched_discussion import BatchError, BatchEvent, BatchPoint, BatchState


def event(state: BatchState, point: str, action: str, **kwargs: str) -> BatchEvent:
    return BatchEvent(
        event_id=f"event-{state.task_revision}-{action}-{point}",
        task_id="AR-0027",
        task_revision=state.task_revision,
        session_id="session-1",
        packet_ref="awg/packet-1",
        point_ref=point,
        anchor_ref=kwargs.get("anchor_ref", f"plan/{point}"),
        action=action,
        response_ref=kwargs.get("response_ref"),
        relation_ref=kwargs.get("relation_ref"),
    )


class BatchedDiscussionTests(unittest.TestCase):
    def state(self) -> BatchState:
        return BatchState(
            "AR-0027",
            1,
            "session-1",
            "awg/packet-1",
            (BatchPoint("point-1", "plan/point-1"), BatchPoint("point-2", "plan/point-2")),
        )

    def test_partial_points_remain_independent(self) -> None:
        state = self.state()
        state.apply(event(state, "point-1", "focus"))
        state.apply(event(state, "point-1", "respond", response_ref="awg/response-1"))
        self.assertEqual(("awg/response-1", False, 0), state.point("point-1"))
        self.assertEqual((None, True, 0), state.point("point-2"))

    def test_coupled_points_require_relation_before_response(self) -> None:
        state = BatchState(
            "AR-0027",
            1,
            "session-1",
            "awg/packet-1",
            (
                BatchPoint("point-1", "plan/point-1", "relation-1"),
                BatchPoint("point-2", "plan/point-2", "relation-1"),
            ),
        )
        with self.assertRaisesRegex(BatchError, "declared"):
            state.apply(event(state, "point-1", "respond", response_ref="response-1"))
        state.apply(event(state, "point-1", "declare_coupling", relation_ref="relation-1"))
        state.apply(event(state, "point-1", "respond", response_ref="response-1"))
        self.assertFalse(state.point("point-1")[1])
        self.assertTrue(state.point("point-2")[1])

    def test_stale_anchor_duplicate_and_reask_are_fail_closed(self) -> None:
        state = self.state()
        with self.assertRaisesRegex(BatchError, "anchor"):
            state.apply(event(state, "point-1", "focus", anchor_ref="plan/other"))
        focus = event(state, "point-1", "focus")
        state.apply(focus)
        with self.assertRaisesRegex(BatchError, "duplicate"):
            state.apply(focus)
        state.apply(event(state, "point-1", "respond", response_ref="response-1"))
        state.apply(event(state, "point-1", "reask"))
        self.assertEqual((None, True, 1), state.point("point-1"))

    def test_batch_shapes_and_session_fences_are_fail_closed(self) -> None:
        with self.assertRaisesRegex(BatchError, "public-safe"):
            BatchPoint("../private", "plan/point-1")
        with self.assertRaisesRegex(BatchError, "coupled_to"):
            BatchPoint("point-1", "plan/point-1", "../relation")
        base = {
            "event_id": "event-1",
            "task_id": "AR-0027",
            "task_revision": 1,
            "session_id": "session-1",
            "packet_ref": "awg/packet-1",
            "point_ref": "point-1",
            "anchor_ref": "plan/point-1",
            "action": "focus",
        }
        for key, value, message in (
            ("task_id", "bad", "task_id"),
            ("task_revision", 0, "positive"),
            ("action", "invalid", "action"),
            ("response_ref", "../private", "public-safe"),
        ):
            with self.subTest(key=key), self.assertRaisesRegex(BatchError, message):
                BatchEvent(**{**base, key: value})
        with self.assertRaisesRegex(BatchError, "identity"):
            BatchState("bad", 1, "session-1", "awg/packet-1", (BatchPoint("p", "a"),))
        with self.assertRaisesRegex(BatchError, "unique"):
            BatchState(
                "AR-0027",
                1,
                "session-1",
                "awg/packet-1",
                (BatchPoint("p", "a"), BatchPoint("p", "b")),
            )

        state = self.state()
        wrong = BatchEvent(
            "x", "AR-0099", 1, "session-1", "awg/packet-1", "point-1", "plan/point-1", "focus"
        )
        with self.assertRaisesRegex(BatchError, "stale batch session"):
            state.apply(wrong)
        wrong = BatchEvent(
            "x", "AR-0027", 1, "other", "awg/packet-1", "point-1", "plan/point-1", "focus"
        )
        with self.assertRaisesRegex(BatchError, "stale batch session"):
            state.apply(wrong)
        wrong = BatchEvent(
            "x", "AR-0027", 1, "session-1", "other", "point-1", "plan/point-1", "focus"
        )
        with self.assertRaisesRegex(BatchError, "packet"):
            state.apply(wrong)
        with self.assertRaisesRegex(BatchError, "unknown"):
            state.apply(event(state, "unknown", "focus"))
        with self.assertRaisesRegex(BatchError, "unknown"):
            state.point("unknown")

    def test_coupling_and_response_rejections_are_fail_closed(self) -> None:
        state = BatchState(
            "AR-0027",
            1,
            "session-1",
            "awg/packet-1",
            (BatchPoint("point-1", "plan/point-1", "relation-1"),),
        )
        with self.assertRaisesRegex(BatchError, "declared"):
            state.apply(event(state, "point-1", "declare_coupling", relation_ref="wrong"))
        with self.assertRaisesRegex(BatchError, "required"):
            state.apply(event(state, "point-1", "respond"))
        with self.assertRaisesRegex(BatchError, "stale batch event revision"):
            state.apply(
                BatchEvent(
                    "stale",
                    "AR-0027",
                    9,
                    "session-1",
                    "awg/packet-1",
                    "point-1",
                    "plan/point-1",
                    "focus",
                )
            )


if __name__ == "__main__":
    unittest.main()
