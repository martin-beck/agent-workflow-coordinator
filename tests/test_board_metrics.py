# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Contract tests for the deterministic company board projection."""

import json
import unittest
from pathlib import Path

from tools.board_metrics import build_metrics, encode


def task(task_id: str, **fields: object) -> tuple[Path, dict[str, object], str]:
    meta: dict[str, object] = {
        "id": task_id,
        "status": "open",
        "priority": "P1",
        "role": "implementer",
        "team": "coordination",
    }
    meta.update(fields)
    return Path(f"{task_id}.md"), meta, "private body must not appear"


class BoardMetricsTests(unittest.TestCase):
    def test_projection_covers_company_metrics_and_is_byte_stable(self) -> None:
        tasks = [
            task("AR-0002", status="blocked", role="reviewer"),
            task(
                "AR-0001",
                oracle_gate={
                    "required": True,
                    "stage_sequence": ["role", "spec", "decision"],
                    "open_stage": "decision",
                    "completed": ["role", "spec"],
                    "authorized": False,
                    "reconciliation_required": False,
                },
                spec_acceptance={"status": "pass", "evidence_ref": "AR-0001/tests"},
            ),
        ]
        result = build_metrics(list(reversed(tasks)))
        self.assertEqual(["AR-0001"], result["decision_backlog"]["tasks"])
        self.assertEqual(1, result["gate_failures"]["count"])
        self.assertEqual(["AR-0002"], [item["task"] for item in result["blocked_tasks"]["items"]])
        self.assertEqual(
            {"covered": 1, "total": 2, "tasks": ["AR-0001"]}, result["evidence_coverage"]
        )
        self.assertEqual(encode(result), encode(build_metrics(tasks)))
        self.assertNotIn("private body", encode(result))
        self.assertEqual(result, json.loads(encode(result)))

    def test_unrequired_or_fully_authorized_gate_is_not_a_failure(self) -> None:
        tasks = [
            task("AR-0001"),
            task(
                "AR-0002",
                oracle_gate={
                    "required": True,
                    "stage_sequence": ["role", "spec", "decision"],
                    "open_stage": None,
                    "completed": ["role", "spec", "decision"],
                    "authorized": True,
                    "reconciliation_required": False,
                },
            ),
        ]
        result = build_metrics(tasks)
        self.assertEqual(0, result["decision_backlog"]["count"])
        self.assertEqual(0, result["gate_failures"]["count"])


if __name__ == "__main__":
    unittest.main()
