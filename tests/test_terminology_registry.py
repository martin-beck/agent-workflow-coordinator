# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Contract tests for the coordinator-owned terminology registry."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
REGISTRY = ROOT / "quality" / "terminology.json"
BROKEN_FIXTURE = ROOT / "fixtures" / "broken" / "terminology" / "docs" / "incorrect.md.fixture"


class TerminologyRegistryTests(unittest.TestCase):
    def test_registry_covers_required_coordinator_terms(self) -> None:
        document = json.loads(REGISTRY.read_text(encoding="utf-8"))
        terms = {item["canonical"]: item for item in document["terms"]}
        self.assertEqual(
            {
                "task",
                "status",
                "owner",
                "claim",
                "lease",
                "revision",
                "backend authority",
                "projection",
                "binding",
                "reconciliation",
                "vendor sync",
                "task release",
                "software release",
            },
            set(terms),
        )
        self.assertEqual(len(document["terms"]), len({item["id"] for item in document["terms"]}))

    def test_negative_fixture_contains_a_declared_forbidden_alias(self) -> None:
        document = json.loads(REGISTRY.read_text(encoding="utf-8"))
        aliases = {alias for item in document["terms"] for alias in item["aliases"]}
        self.assertIn("work item", aliases)
        self.assertIn("work item", BROKEN_FIXTURE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
