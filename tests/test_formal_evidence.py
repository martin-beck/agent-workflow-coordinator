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
    def test_sqlite_correspondence_implementation_symbols_resolve(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        source = (ROOT / artifact["implementation"]["module"]).read_text(encoding="utf-8")
        for transition in artifact["transitions"]:
            for symbol in transition["implementation"]:
                name = symbol.rsplit(".", 1)[-1]
                self.assertIsNotNone(
                    re.search(rf"\bdef {re.escape(name)}\(", source),
                    f"{transition['name']}: {symbol}",
                )

    def test_sqlite_correspondence_evidence_references_resolve_to_tests(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        for reference in artifact["evidence"]["tests"]:
            relative, qualified = reference.split("::", 1)
            _class_name, method_name = qualified.split(".", 1)
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIsNotNone(
                re.search(rf"^    def {re.escape(method_name)}\(", source, re.MULTILINE),
                reference,
            )

    def test_sqlite_snapshot_correspondence_map_is_bounded_and_fail_closed(self) -> None:
        value = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        self.assertEqual("bounded-sqlite-snapshot-correspondence", value["kind"])
        self.assertEqual("formal/upgrade/UpgradeRecovery.tla", value["model"]["path"])
        self.assertEqual("rejection-only", value["implementation"]["mutation_gate"])
        transitions = {item["name"]: item for item in value["transitions"]}
        self.assertEqual(
            {"snapshot-read", "identity-reread", "read-or-close-uncertainty", "ambiguous-fence"},
            set(transitions),
        )
        uncertainty = transitions["read-or-close-uncertainty"]["model"]
        self.assertEqual("Crash", uncertainty["action"])
        self.assertEqual("safe_mode", uncertainty["journal_after"])
        self.assertEqual("ambiguous", uncertainty["barrier_after"])
        self.assertEqual("reject", transitions["ambiguous-fence"]["model"]["result"])
        self.assertEqual("bounded-trace-map-only", value["evidence"]["status"])
        self.assertEqual("not-proven", value["evidence"]["correspondence_claim"])
        self.assertTrue(value["evidence"]["nonclaims"])

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
        models.add("OracleInteractionGates")
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
        self.assertEqual(
            ["HandoffctlFast", "OracleInteractionGates"],
            manifest["profiles"]["pr-fast"]["models"],
        )
        self.assertIn("safety-only", manifest["profiles"]["pr-fast"]["claims"])
        fast_config = (FORMAL_ROOT / "HandoffctlFast.cfg").read_text()
        self.assertIn("SPECIFICATION Spec", fast_config)
        self.assertNotIn("PROPERTIES", fast_config)
        self.assertNotIn("EventuallyBoundCallSucceeds", fast_config)
        fast_invocations = re.findall(
            r"^\s*run_model\s+(\w+)(?:\s+(\w+))?\s*$", verify, re.MULTILINE
        )
        self.assertIn(
            ("HandoffctlFast", ""), [(model, source or "") for model, source in fast_invocations]
        )
        self.assertIn("run_model OracleInteractionGates", verify)
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
        formal = (ROOT / ".github" / "workflows" / "formal.yml").read_text()
        self.assertIn(
            "github.event.pull_request.head.repo.full_name != github.repository", workflow
        )
        self.assertIn("TLC_TIER: portable-smoke", workflow)
        self.assertIn("TLC_CGROUP_MODE: portable", workflow)
        self.assertIn("release_sensitive", workflow)
        for release_path in (
            "pyproject.toml",
            "uv.lock",
            "CHANGELOG.md",
            "tools/vendor.py",
        ):
            self.assertIn(release_path, workflow)
        self.assertIn("tools/lifecycle_trace.py", workflow)
        self.assertIn("push:", formal)
        self.assertIn("workflow_dispatch:", formal)
        self.assertIn("schedule:", formal)
        self.assertIn("if: github.ref == 'refs/heads/main'", formal)
        self.assertIn("runs-on: [self-hosted, Linux, X64, agent-workflow-coordinator-ci]", formal)
        formal_step = formal[
            formal.index("      - name: Run required formal tier\n") : formal.index(
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
        self.assertIn("MemoryMax=6G", formal)
        self.assertIn("MemorySwapMax=6G", formal)
        self.assertIn("MemTotal", formal)
        self.assertIn("/proc/self/cgroup", formal)
        self.assertIn("/proc/self/mountinfo", formal)
        self.assertIn('cgroup_dir="${cgroup_mount%/}${cgroup_relative:-/}"', formal)
        self.assertIn('"${cgroup_dir}/memory.max"', formal)
        self.assertIn('"${cgroup_dir}/memory.swap.max"', formal)
        self.assertNotIn("needs.scope.outputs.release_sensitive", formal_step)
        self.assertIn("github.event_name == 'schedule' && 360", formal)
        timeout_expression = formal[
            formal.index("TLC_TIMEOUT_SECONDS:") : formal.index(
                "\n", formal.index("TLC_TIMEOUT_SECONDS:")
            )
        ]
        self.assertIn("'6000'", timeout_expression)
        self.assertIn("'1200'", timeout_expression)

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
