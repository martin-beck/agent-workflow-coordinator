# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Deterministic end-to-end qualification of the formal-tier migration.

These tests deliberately stop at the runner outcome manifest.  TLC itself is
covered by the tier runners; this suite verifies the boundary which turns a
completed bounded run into locally consumable evidence without depending on a
hosted workflow, identity, or artifact service.
"""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from tools.formal_receipt import FormalReceiptError, expected_candidate, validate_receipt

ROOT = Path(__file__).resolve().parent.parent
ATTEST = ROOT / "formal/handoffctl/attest.py"
TIER = "pr-publication"


class FormalTierQualificationTests(unittest.TestCase):
    def render_receipt(
        self, *, tier: str = TIER, classification: str = "canonical"
    ) -> dict[str, Any]:
        """Render an attestation from a controlled successful runner projection."""
        expected = runpy.run_path(str(ATTEST))["EXPECTED_MODELS"]
        models = sorted(expected[tier])
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            jar = temporary / "tlc.jar"
            manifest = temporary / "outcomes.txt"
            receipt = temporary / "receipt.json"
            jar.write_bytes(b"controlled-tlc-jar")
            manifest.write_text("".join(f"{model} success\n" for model in models), encoding="utf-8")
            environment = {
                **os.environ,
                "TLC_CGROUP_MODE": "required",
                "TLC_MEMORY_MAX": "3G",
                "TLC_SWAP_MAX": "3G",
            }
            subprocess.run(  # noqa: S603 -- fixed local Python script and controlled fixture paths
                [
                    sys.executable,
                    str(ATTEST),
                    "--tier",
                    tier,
                    "--output",
                    str(receipt),
                    "--jar",
                    str(jar),
                    "--models",
                    *models,
                    "--manifest",
                    str(manifest),
                    "--budget-seconds",
                    str(runpy.run_path(str(ATTEST))["TIER_BUDGET_SECONDS"][tier]),
                    "--started-epoch",
                    str(int(__import__("time").time())),
                    "--execution-classification",
                    classification,
                ],
                cwd=ROOT,
                env=environment,
                check=True,
            )
            rendered = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertIsInstance(rendered, dict)
            return cast(dict[str, Any], rendered)

    def test_bounded_candidate_receipt_renders_and_validates_at_exact_head(self) -> None:
        receipt = self.render_receipt()
        candidate = expected_candidate(ROOT)
        self.assertEqual(candidate, receipt["commit"])
        self.assertEqual(candidate, receipt["candidate_identity"]["commit"])
        self.assertEqual("canonical", receipt["execution_classification"])
        self.assertTrue(receipt["canonical_publication_evidence"])
        validate_receipt(receipt, root=ROOT, candidate=candidate, tier=TIER)

    def test_typed_resource_and_timeout_outcomes_reject_the_rendered_projection(self) -> None:
        receipt = self.render_receipt()
        outcomes = receipt["outcomes"]
        assert isinstance(outcomes, dict)
        outcomes["HandoffctlStorage"] = "admission-failed"
        with self.assertRaisesRegex(FormalReceiptError, "resource exhaustion"):
            validate_receipt(receipt, root=ROOT, candidate=expected_candidate(ROOT), tier=TIER)

        receipt = self.render_receipt()
        receipt["status"] = "deadline-exceeded"
        with self.assertRaisesRegex(FormalReceiptError, "timeout"):
            validate_receipt(receipt, root=ROOT, candidate=expected_candidate(ROOT), tier=TIER)

    def test_advisory_stress_receipt_is_noncanonical_and_separately_cancellable(self) -> None:
        receipt = self.render_receipt(
            tier="full-exhaustive", classification="diagnostic-private-admission"
        )
        self.assertFalse(receipt["canonical_publication_evidence"])
        self.assertIn("exploratory", " ".join(receipt["non_claims"]))
        self.assertIn("private-admission", " ".join(receipt["non_claims"]))

        bounded = (ROOT / ".github/workflows/formal.yml").read_text(encoding="utf-8")
        stress = (ROOT / ".github/workflows/formal-stress.yml").read_text(encoding="utf-8")
        self.assertIn("cancel-in-progress: false", bounded)
        self.assertIn("cancel-in-progress: false", stress)
        self.assertIn("agent-workflow-coordinator-ci", bounded)
        self.assertIn("agent-workflow-coordinator-formal-stress", stress)
        self.assertIn("if: github.ref == 'refs/heads/main'", bounded)
        self.assertIn("if: github.ref == 'refs/heads/main'", stress)
        self.assertNotIn("pull_request:", bounded)
        self.assertNotIn("continue-on-error: true", bounded)
        self.assertNotIn("continue-on-error: true", stress)
        self.assertIn("advisory-formal-stress", stress)

    def test_legacy_liveness_bootstrap_has_a_one_sync_compatibility_contract(self) -> None:
        readme = (ROOT / "formal/handoffctl/README.md").read_text(encoding="utf-8")
        vendor = (ROOT / "tools/vendor.py").read_text(encoding="utf-8")
        self.assertIn("source-only compatibility bootstrap", readme)
        self.assertIn(
            "new updater removes the bootstrap on its next atomic sync",
            " ".join(readme.split()),
        )
        self.assertIn('"formal/handoffctl/HandoffctlPR.cfg",', vendor)
        self.assertIn("RETIRED_DESTINATIONS", vendor)
        self.assertIn("retire_staged_destinations", vendor)


if __name__ == "__main__":
    unittest.main()
