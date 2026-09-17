# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Complete initiator-path and hostile bypass acceptance tests."""

import unittest
from typing import cast

from tools.tui_integration import (
    TRACE_STEPS,
    Initiator,
    TuiAcceptanceTrace,
    TuiIntegrationError,
)


def trace(initiator: Initiator = Initiator.USER, **changes: object) -> TuiAcceptanceTrace:
    return TuiAcceptanceTrace(
        task_id=str(changes.get("task_id", "AR-0029")),
        task_revision=cast(int, changes.get("task_revision", 1)),
        initiator=initiator,
        steps=cast(tuple[str, ...], changes.get("steps", TRACE_STEPS)),
        formal_spec_ref=str(changes.get("formal_spec_ref", "awq/formal/trace-1")),
        user_disposition_ref=str(changes.get("user_disposition_ref", "awg/decision/1")),
        quality_evidence_ref=str(changes.get("quality_evidence_ref", "awq/quality/trace-1")),
        future_ar_ref=str(changes.get("future_ar_ref", "AR-0030")),
        implementation_ref=str(changes.get("implementation_ref", "coordinator/handoff/1")),
        decision_authority=str(changes.get("decision_authority", "coordinator")),
    )


class TuiIntegrationTests(unittest.TestCase):
    def test_agent_and_user_initiated_paths_complete(self) -> None:
        for initiator in Initiator:
            with self.subTest(initiator=initiator):
                result = trace(initiator).accept()
                self.assertEqual(Initiator(initiator), result.initiator)
                self.assertEqual(12, result.final_revision)
                self.assertEqual("AR-0030", result.future_ar_ref)

    def test_every_lifecycle_bypass_is_rejected(self) -> None:
        for skipped in ("formal_specification", "user_disposition", "reconciliation"):
            with (
                self.subTest(skipped=skipped),
                self.assertRaisesRegex(TuiIntegrationError, skipped.replace("_", " ")),
            ):
                trace(steps=tuple(step for step in TRACE_STEPS if step != skipped)).accept()
        with self.assertRaisesRegex(TuiIntegrationError, "Coordinator"):
            trace(decision_authority="awg").accept()

    def test_quality_and_public_evidence_boundaries_are_separate(self) -> None:
        with self.assertRaisesRegex(TuiIntegrationError, "public-safe"):
            trace(quality_evidence_ref="/private/transcript").accept()
        with self.assertRaisesRegex(TuiIntegrationError, "future AR"):
            trace(future_ar_ref="future/request-without-ar").accept()
        result = trace().accept()
        self.assertNotEqual(result.contract_digest, "sha256:" + "0" * 64)


if __name__ == "__main__":
    unittest.main()
