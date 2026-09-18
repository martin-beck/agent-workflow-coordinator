# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

import unittest

from tools.decision_routing import DecisionRoutingError, assess_decision, require_tui_route


def trigger(ref: str = "AWG-AR-0001") -> dict[str, object]:
    return {"interaction_required": True, "decision_request_ref": ref}


class DecisionRoutingTests(unittest.TestCase):
    def test_design_choice_always_requires_tui(self) -> None:
        assessment = assess_decision(decision_class="design")
        self.assertTrue(assessment.requires_tui)
        require_tui_route(assessment, trigger=trigger(), request_ref="AWG-AR-0001", channel="workflow-tui")

    def test_high_impact_operational_choice_requires_tui(self) -> None:
        assessment = assess_decision(decision_class="operational", impact="high")
        with self.assertRaisesRegex(DecisionRoutingError, "workflow-tui"):
            require_tui_route(
                assessment, trigger=trigger(), request_ref="AWG-AR-0001", channel="codex-host", host_direct_question=True
            )

    def test_uncertainty_without_trigger_fails_closed(self) -> None:
        assessment = assess_decision(decision_class="operational", uncertainty=True)
        with self.assertRaisesRegex(DecisionRoutingError, "active human trigger"):
            require_tui_route(assessment, trigger=None, request_ref="AWG-AR-0001", channel="workflow-tui")

    def test_routine_choice_can_continue_without_tui(self) -> None:
        assessment = assess_decision(decision_class="operational")
        require_tui_route(assessment, trigger=None, request_ref=None, channel="codex-host")

    def test_mismatched_reference_is_rejected(self) -> None:
        assessment = assess_decision(decision_class="conceptual")
        with self.assertRaisesRegex(DecisionRoutingError, "references"):
            require_tui_route(assessment, trigger=trigger(), request_ref="AWG-OTHER", channel="workflow-tui")


if __name__ == "__main__":
    unittest.main()
