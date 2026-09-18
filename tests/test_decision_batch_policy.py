# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Tests for autonomous progress planning before human escalation."""

from __future__ import annotations

import unittest

from tools.decision_batch_policy import ARDecision, DecisionBatchError, plan_progress


def decision(
    ar_id: str, *, state: str = "ready", human: bool = False, key: str | None = None
) -> ARDecision:
    return ARDecision(
        ar_id,
        1,
        state,
        human,
        key,
        f"AWG-{ar_id}" if human else None,
    )


class DecisionBatchPolicyTests(unittest.TestCase):
    def test_safe_independent_work_continues_before_tui(self) -> None:
        plan = plan_progress(
            (
                decision("AR-0001"),
                decision("AR-0002", human=True, key="session-1"),
                decision("AR-0003"),
            )
        )
        self.assertEqual(plan.autonomous_ar_ids, ("AR-0001", "AR-0003"))
        self.assertEqual(plan.human_batches[0].ar_ids, ("AR-0002",))

    def test_explicit_batch_groups_only_human_requests(self) -> None:
        plan = plan_progress(
            (
                decision("AR-0001", human=True, key="session-1"),
                decision("AR-0002", human=True, key="session-1"),
                decision("AR-0003", human=True, key="session-2"),
            )
        )
        self.assertEqual(
            [batch.batch_key for batch in plan.human_batches], ["session-1", "session-2"]
        )
        self.assertEqual(
            plan.human_batches[0].decision_request_refs, ("AWG-AR-0001", "AWG-AR-0002")
        )

    def test_missing_batch_key_is_not_inferred(self) -> None:
        plan = plan_progress((decision("AR-0001", human=True), decision("AR-0002", human=True)))
        self.assertEqual(
            [batch.ar_ids for batch in plan.human_batches], [("AR-0001",), ("AR-0002",)]
        )

    def test_blocked_work_is_neither_run_nor_presented(self) -> None:
        plan = plan_progress((decision("AR-0001", state="blocked"),))
        self.assertEqual(plan, plan.__class__((), ()))

    def test_human_request_requires_reference_and_duplicate_is_rejected(self) -> None:
        with self.assertRaisesRegex(DecisionBatchError, "request reference"):
            ARDecision("AR-0001", 1, "uncertain", True)
        with self.assertRaisesRegex(DecisionBatchError, "unique"):
            plan_progress((decision("AR-0001"), decision("AR-0001")))


if __name__ == "__main__":
    unittest.main()
