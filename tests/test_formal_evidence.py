# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Tests for the structured bounded-model evidence contract."""

from __future__ import annotations

import io
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
        tier_manifest = json.loads((ROOT / "formal" / "tier-evidence.json").read_text())
        full_models = tier_manifest["profiles"]["full-exhaustive"]["models"]
        configs = [FORMAL_ROOT / f"{model}.cfg" for model in full_models]
        models = {config.stem for config in FORMAL_ROOT.glob("*.cfg")}
        runner = (FORMAL_ROOT / "verify.sh").read_text(encoding="utf-8")
        invoked = set(re.findall(r"^\s*run_model\s+(\w+)(?:\s+\w+)?\s*$", runner, re.MULTILINE))

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
            "pr_runtime_max_seconds",
        ):
            self.assertIsInstance(bounds[name], int)
            self.assertGreater(bounds[name], 0)

    def test_formal_tiers_require_explicit_non_ambiguous_selection(self) -> None:
        verify = (FORMAL_ROOT / "verify.sh").read_text(encoding="utf-8")
        self.assertIn("--tier", verify)
        self.assertIn("portable-smoke", verify)
        self.assertIn("pr-fast", verify)
        self.assertIn("pr-publication", verify)
        self.assertIn("full-exhaustive", verify)
        manifest = json.loads((ROOT / "formal" / "tier-evidence.json").read_text())
        self.assertFalse(manifest["profiles"]["portable-smoke"]["exhaustive"])
        self.assertFalse(manifest["profiles"]["pr-fast"]["exhaustive"])
        self.assertEqual(["HandoffctlFast"], manifest["profiles"]["pr-fast"]["models"])
        self.assertIn("safety-only", manifest["profiles"]["pr-fast"]["claims"])
        fast_config = (FORMAL_ROOT / "HandoffctlFast.cfg").read_text()
        self.assertIn("SPECIFICATION Spec", fast_config)
        self.assertIn("PROPERTIES", fast_config)
        self.assertIn("EventuallyBoundCallSucceeds", fast_config)
        self.assertFalse(manifest["profiles"]["pr-publication"]["exhaustive"])
        self.assertTrue(manifest["profiles"]["full-exhaustive"]["exhaustive"])
        self.assertNotEqual(
            manifest["profiles"]["portable-smoke"]["models"],
            manifest["profiles"]["full-exhaustive"]["models"],
        )
        self.assertEqual(6, len(manifest["profiles"]["full-exhaustive"]["models"]))
        self.assertEqual(6, len(manifest["profiles"]["pr-publication"]["models"]))
        pr_config = (FORMAL_ROOT / "HandoffctlPR.cfg").read_text()
        self.assertIn("Processes = {p1}", pr_config)
        self.assertIn("EventualCompletion", pr_config)
        attest = (ROOT / "formal" / "handoffctl" / "attest.py").read_text(encoding="utf-8")
        self.assertIn("state_counts", attest)
        self.assertIn("state_counts are unavailable", attest)
        self.assertIn("of exhaustive exploration", attest)
        self.assertIn("attestation requires TLC_CGROUP_MODE=required", attest)
        self.assertIn("runner-produced outcome manifest", attest)

    def test_workflow_separates_fork_pr_publication_and_weekly_tiers(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "verify.yml").read_text()
        self.assertIn(
            "github.event.pull_request.head.repo.full_name != github.repository", workflow
        )
        self.assertIn("&& 'portable-smoke'", workflow)
        self.assertIn("|| 'pr-fast'", workflow)
        self.assertIn("&& 'full-exhaustive'", workflow)
        self.assertIn("timeout-minutes: ${{", workflow)
        self.assertIn("continue-on-error: ${{ github.event_name == 'schedule' }}", workflow)
        self.assertIn("release_sensitive", workflow)
        for release_path in ("pyproject.toml", "uv.lock", "CHANGELOG.md", "tools/vendor.py"):
            self.assertIn(release_path, workflow)
        tier_expression = workflow[
            workflow.index("      TLC_TIER:") : workflow.index(
                "    steps:", workflow.index("  verify:")
            )
        ]
        fork_guard = "github.event.pull_request.head.repo.full_name != github.repository"
        self.assertLess(
            tier_expression.index(fork_guard), tier_expression.index("'full-exhaustive'")
        )
        steps_start = workflow.index("    steps:\n", workflow.index("  verify:\n"))
        job_environment = workflow[
            workflow.index("    env:\n", workflow.index("  verify:\n")) : steps_start
        ]
        for resource_setting in (
            "TLC_CGROUP_MODE",
            "TLC_HEAP",
            "TLC_MEMORY_MAX",
            "TLC_SWAP_MAX",
            "TLC_TIMEOUT_SECONDS",
        ):
            self.assertNotIn(resource_setting, job_environment)
        formal_step = workflow[
            workflow.index("      - name: Run event-appropriate formal tier\n") : workflow.index(
                "      - name: Publish exact-head tier attestation\n"
            )
        ]
        for resource_setting in (
            "TLC_CGROUP_MODE",
            "TLC_HEAP",
            "TLC_MEMORY_MAX",
            "TLC_SWAP_MAX",
            "TLC_TIMEOUT_SECONDS",
        ):
            self.assertIn(resource_setting, formal_step)
        self.assertIn("MemoryMax=6G", workflow)
        self.assertIn("MemorySwapMax=6G", workflow)
        self.assertIn("MemTotal", workflow)
        self.assertIn("/proc/self/cgroup", workflow)
        self.assertIn("/proc/self/mountinfo", workflow)
        self.assertIn('cgroup_dir="${cgroup_mount%/}${cgroup_relative:-/}"', workflow)
        self.assertIn('"${cgroup_dir}/memory.max"', workflow)
        self.assertIn('"${cgroup_dir}/memory.swap.max"', workflow)
        self.assertIn("needs.scope.outputs.release_sensitive == 'true') && '6000'", formal_step)
        self.assertIn("github.event_name == 'schedule' || github.event_name == 'workflow_dispatch') && 360", workflow)
        timeout_expression = workflow[
            workflow.index("TLC_TIMEOUT_SECONDS:") : workflow.index(
                "\n", workflow.index("TLC_TIMEOUT_SECONDS:")
            )
        ]
        self.assertLess(
            timeout_expression.index(
                "github.event.pull_request.head.repo.full_name != github.repository"
            ),
            timeout_expression.index("'6000'"),
        )

    def test_attestation_rejects_failed_formal_outcomes(self) -> None:
        script = ROOT / "formal" / "handoffctl" / "attest.py"
        stderr = io.StringIO()
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
                    "--manifest",
                    str(script),
                    "--models",
                    "HandoffctlBinding",
                ],
            ),
            mock.patch.object(sys, "stderr", stderr),
            self.assertRaises(SystemExit),
        ):
            runpy.run_path(str(script), run_name="__main__")
        self.assertIn("failed or incomplete formal runs", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
