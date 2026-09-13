# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Tests for the structured bounded-model evidence contract."""

from __future__ import annotations

import json
import re
import runpy
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FORMAL_ROOT = ROOT / "formal" / "handoffctl"
SET_BOUND_NAMES = {
    "Actors": "max_actors",
    "Processes": "max_processes",
    "Projects": "max_projects",
    "Tasks": "max_tasks",
    "Worktrees": "max_worktrees",
}


def load_evidence() -> dict[str, Any]:
    value = json.loads((ROOT / "formal" / "evidence.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("formal evidence must be a JSON object")
    return value


def configured_bounds(configs: list[Path]) -> dict[str, int]:
    bounds = dict.fromkeys(SET_BOUND_NAMES.values(), 0)
    bounds["max_revision"] = 0
    for config in configs:
        text = config.read_text(encoding="utf-8")
        for constant, name in SET_BOUND_NAMES.items():
            match = re.search(rf"^\s*{constant}\s*=\s*\{{([^}}]+)\}}", text, re.MULTILINE)
            if match is not None:
                size = len([item for item in match.group(1).split(",") if item.strip()])
                bounds[name] = max(bounds[name], size)
        revision = re.search(r"^\s*MaxRevision\s*=\s*(\d+)", text, re.MULTILINE)
        if revision is not None:
            bounds["max_revision"] = max(bounds["max_revision"], int(revision.group(1)))
    bounds["model_count"] = len(configs)
    return {name: value for name, value in bounds.items() if value > 0}


class FormalEvidenceTests(unittest.TestCase):
    def test_evidence_has_exact_awq_v024_contract_fields(self) -> None:
        evidence = load_evidence()
        self.assertEqual(
            {
                "assumptions",
                "bounds",
                "correspondence",
                "evidence_class",
                "limitations",
                "non_claims",
                "schema_version",
                "scope",
            },
            set(evidence),
        )
        self.assertEqual(1, evidence["schema_version"])
        self.assertEqual("bounded-model", evidence["evidence_class"])
        self.assertEqual("handoffctl-coordination-models", evidence["scope"])
        self.assertEqual("not-proven", evidence["correspondence"])
        for field in ("assumptions", "limitations", "non_claims"):
            values = evidence[field]
            self.assertIsInstance(values, list)
            self.assertTrue(values)
            self.assertEqual(len(values), len(set(values)))

    def test_evidence_bounds_and_runner_match_tracked_models(self) -> None:
        configs = sorted(FORMAL_ROOT.glob("*.cfg"))
        models = {config.stem for config in configs}
        runner = (FORMAL_ROOT / "verify.sh").read_text(encoding="utf-8")
        invoked = set(re.findall(r"^\s*run_model\s+(\w+)\s*$", runner, re.MULTILINE))

        self.assertEqual(models, invoked)
        bounds = load_evidence()["bounds"]
        model_bounds = configured_bounds(configs)
        self.assertEqual(model_bounds, {name: bounds[name] for name in model_bounds})
        for name in (
            "tlc_workers",
            "jvm_heap_mb",
            "hosted_portable_jvm_heap_mb",
            "memory_max_mb",
            "swap_max_mb",
            "cpu_quota_percent",
            "tasks_max",
            "runtime_max_seconds",
        ):
            self.assertIsInstance(bounds[name], int)
            self.assertGreater(bounds[name], 0)

    def test_formal_tiers_require_explicit_non_ambiguous_selection(self) -> None:
        verify = (FORMAL_ROOT / "verify.sh").read_text(encoding="utf-8")
        self.assertIn("--tier", verify)
        self.assertIn("portable-smoke", verify)
        self.assertIn("full-exhaustive", verify)
        manifest = json.loads((ROOT / "formal" / "tier-evidence.json").read_text())
        self.assertFalse(manifest["profiles"]["portable-smoke"]["exhaustive"])
        self.assertTrue(manifest["profiles"]["full-exhaustive"]["exhaustive"])
        self.assertNotEqual(
            manifest["profiles"]["portable-smoke"]["models"],
            manifest["profiles"]["full-exhaustive"]["models"],
        )
        self.assertEqual(6, len(manifest["profiles"]["full-exhaustive"]["models"]))
        attest = (ROOT / "formal" / "handoffctl" / "attest.py").read_text(encoding="utf-8")
        self.assertIn("state_counts", attest)
        self.assertIn("not evidence of exhaustive exploration", attest)
        self.assertIn("requires TLC_CGROUP_MODE=required", attest)
        self.assertIn("runner-produced outcome manifest", attest)

    def test_attestation_rejects_failed_formal_outcomes(self) -> None:
        script = ROOT / "formal" / "handoffctl" / "attest.py"
        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    str(script),
                    "--tier",
                    "portable-smoke",
                    "--status",
                    "oom",
                    "--output",
                    str(ROOT / "formal" / "_unused.json"),
                    "--jar",
                    str(script),
                    "--models",
                    "HandoffctlBinding",
                ],
            ),
            self.assertRaises(SystemExit),
        ):
            runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    unittest.main()
