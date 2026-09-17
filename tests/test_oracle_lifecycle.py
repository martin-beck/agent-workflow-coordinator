# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Hostile and positive contract tests for mandatory interaction gates."""

import unittest

from tools.oracle_lifecycle import (
    ArtifactRef,
    GateError,
    GateStage,
    InteractionEvent,
    apply_event,
    gate_errors,
    transition_allowed,
)
from tools.oracle_lifecycle_model import check_bounded_model


def event(task_revision: int, stage: GateStage, action: str, disposition: str) -> InteractionEvent:
    return InteractionEvent(
        task_id="AR-0022",
        task_revision=task_revision,
        stage=stage,
        action=action,
        disposition=disposition,
        before=(ArtifactRef("plan/before", "sha256:" + "a" * 64),),
        after=(ArtifactRef("plan/after", "sha256:" + "b" * 64),),
        public_ref="oracle/decision-1",
        recorded_at="2026-09-17T00:00:00+00:00",
    )


class OracleLifecycleTests(unittest.TestCase):
    def test_bounded_model_covers_hostile_and_accepted_traces(self) -> None:
        result = check_bounded_model()
        self.assertGreater(result["accepted"], 0)
        self.assertGreater(result["hostile"], 0)

    def test_complete_ordered_lifecycle_is_typed_and_revision_bound(self) -> None:
        meta = {"id": "AR-0022", "task_revision": 1}
        for stage in GateStage:
            revision = meta["task_revision"]
            apply_event(meta, event(revision, stage, "open", "unresolved"))
            meta["task_revision"] += 1
            apply_event(meta, event(meta["task_revision"], stage, "resolve", "accepted"))
            meta["task_revision"] += 1
        self.assertEqual(
            list(GateStage), [GateStage(item) for item in meta["oracle_gate"]["completed"]]
        )
        self.assertIsNone(meta["oracle_gate"]["open_stage"])

    def test_skipped_gate_and_stale_event_are_rejected(self) -> None:
        meta = {"id": "AR-0022", "task_revision": 3}
        with self.assertRaisesRegex(GateError, "skipped"):
            apply_event(meta, event(3, GateStage.DISCUSSION, "open", "unresolved"))
        with self.assertRaisesRegex(GateError, "stale"):
            apply_event(meta, event(2, GateStage.INTAKE, "open", "unresolved"))

    def test_unresolved_guidance_keeps_gate_open_and_blocks_autonomous_work(self) -> None:
        meta = {"id": "AR-0022", "task_revision": 1}
        apply_event(meta, event(1, GateStage.INTAKE, "open", "unresolved"))
        meta["task_revision"] += 1
        apply_event(meta, event(2, GateStage.INTAKE, "resolve", "unresolved"))
        self.assertEqual("intake", meta["oracle_gate"]["open_stage"])
        with self.assertRaisesRegex(GateError, "unresolved"):
            transition_allowed(meta, "release")
        with self.assertRaisesRegex(GateError, "unresolved"):
            transition_allowed(meta, "run")

    def test_public_safe_artifacts_and_event_shape_are_fail_closed(self) -> None:
        with self.assertRaisesRegex(GateError, "public-safe"):
            ArtifactRef("/private/plan", "sha256:" + "a" * 64)
        self.assertEqual([], gate_errors(None))
        malformed = {
            "required": True,
            "open_stage": "intake",
            "completed": [],
            "events": [{"secret": "not-an-event"}],
        }
        self.assertTrue(gate_errors(malformed))


if __name__ == "__main__":
    unittest.main()
