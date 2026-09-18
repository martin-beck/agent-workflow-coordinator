# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

import unittest

from tools.decision_routing import (
    DecisionRoutingError,
    assess_decision,
    require_all_tui_routes,
    require_tui_route,
)


def trigger(ref: str = "AWG-AR-0001") -> dict[str, object]:
    return {"interaction_required": True, "decision_request_ref": ref}


class DecisionRoutingTests(unittest.TestCase):
    def test_invalid_classification_values_fail_closed(self) -> None:
        cases = (
            ({"decision_class": "unknown"}, "decision_class is invalid"),
            ({"decision_class": "operational", "impact": "unknown"}, "impact is invalid"),
            (
                {"decision_class": "operational", "reversibility": "unknown"},
                "reversibility is invalid",
            ),
        )
        for kwargs, message in cases:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(DecisionRoutingError, message):
                assess_decision(**kwargs)

    def test_all_reason_codes_are_deterministic(self) -> None:
        assessment = assess_decision(
            decision_class="design",
            impact="critical",
            reversibility="irreversible",
            uncertainty=True,
            user_requested=True,
            proposal_review=True,
            policy_required=True,
            project_direction=True,
        )
        self.assertEqual(
            assessment.reason_codes,
            (
                "user-request",
                "proposal-review",
                "agent-uncertainty",
                "policy-required",
                "design-choice",
                "high-impact",
                "irreversible",
                "project-direction",
            ),
        )

    def test_design_choice_always_requires_tui(self) -> None:
        assessment = assess_decision(decision_class="design")
        self.assertTrue(assessment.requires_tui)
        require_tui_route(
            assessment, trigger=trigger(), request_ref="AWG-AR-0001", channel="workflow-ui"
        )

    def test_high_impact_operational_choice_requires_tui(self) -> None:
        assessment = assess_decision(decision_class="operational", impact="high")
        with self.assertRaisesRegex(DecisionRoutingError, "workflow-ui"):
            require_tui_route(
                assessment,
                trigger=trigger(),
                request_ref="AWG-AR-0001",
                channel="codex-host",
                host_direct_question=True,
            )

    def test_uncertainty_without_trigger_fails_closed(self) -> None:
        assessment = assess_decision(decision_class="operational", uncertainty=True)
        with self.assertRaisesRegex(DecisionRoutingError, "active human trigger"):
            require_tui_route(
                assessment, trigger=None, request_ref="AWG-AR-0001", channel="workflow-ui"
            )

    def test_routine_choice_can_continue_without_tui(self) -> None:
        assessment = assess_decision(decision_class="operational")
        require_tui_route(assessment, trigger=None, request_ref=None, channel="codex-host")

    def test_mismatched_reference_is_rejected(self) -> None:
        assessment = assess_decision(decision_class="conceptual")
        with self.assertRaisesRegex(DecisionRoutingError, "references"):
            require_tui_route(
                assessment, trigger=trigger(), request_ref="AWG-OTHER", channel="workflow-ui"
            )

    def test_required_route_rejects_missing_and_malformed_request(self) -> None:
        assessment = assess_decision(decision_class="conceptual")
        for request_ref in (None, "bad-ref"):
            with (
                self.subTest(request_ref=request_ref),
                self.assertRaisesRegex(DecisionRoutingError, "AWG request reference"),
            ):
                require_tui_route(
                    assessment,
                    trigger=trigger(),
                    request_ref=request_ref,
                    channel="workflow-ui",
                )

    def test_batch_route_requires_each_item(self) -> None:
        routine = assess_decision(decision_class="operational")
        important = assess_decision(decision_class="conceptual")
        require_all_tui_routes(((routine, None, None), (important, trigger(), "AWG-AR-0001")))
        with self.assertRaisesRegex(DecisionRoutingError, "active human trigger"):
            require_all_tui_routes(((important, None, "AWG-AR-0001"),))


if __name__ == "__main__":
    unittest.main()
