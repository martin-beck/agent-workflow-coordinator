# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Integrity checks for the bounded upgrade-model evidence snapshot."""

from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "formal" / "upgrade" / "evidence.json"
CONTRACT = ROOT / "formal" / "upgrade" / "v10-refinement-contract.json"


class UpgradeFormalEvidenceTests(unittest.TestCase):
    def test_bound_rollback_inspection_evidence_maps_existing_tests_without_refinement_claim(
        self,
    ) -> None:
        evidence = json.loads(
            (ROOT / "formal" / "upgrade" / "rollback-inspection-evidence.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("implementation-test", evidence["evidence_class"])
        self.assertEqual("not-proven", evidence["implementation_refinement"])
        self.assertEqual(2, len(evidence["formal_obligations"]))
        revision = subprocess.run(  # noqa: S603
            ["git", "rev-parse", evidence["implementation_revision"]],  # noqa: S607
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        tree = subprocess.run(  # noqa: S603
            ["git", "rev-parse", f"{revision}^{{tree}}"],  # noqa: S607
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(tree, evidence["implementation_tree"])
        for tests in evidence["tests"].values():
            for reference in tests:
                path, selector = reference.split("::", 1)
                self.assertTrue((ROOT / path).exists(), path)
                # The file-level check is intentionally lightweight: unittest
                # selectors contain a class and method, while only the method
                # token is expected to occur literally in the source.
                self.assertIn(
                    selector.rsplit("::", 1)[-1],
                    (ROOT / path).read_text(encoding="utf-8"),
                    selector,
                )
        self.assertTrue(any("does not authorize" in claim for claim in evidence["claims"]))

    def test_recorded_model_and_implementation_hashes_match(self) -> None:
        evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))

        def digest(relative: str) -> str:
            return hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()

        self.assertEqual(evidence["model_sha256"], digest(evidence["model"]))
        self.assertEqual(evidence["config_sha256"], digest(evidence["config"]))
        for relative, expected in evidence["implementation_snapshot"]["files"].items():
            self.assertEqual(expected, digest(relative), relative)

    def test_snapshot_revision_is_an_ancestor_of_test_head(self) -> None:
        evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        revision = evidence["implementation_snapshot"]["revision"]
        result = subprocess.run(  # noqa: S603
            ["git", "merge-base", "--is-ancestor", revision, "HEAD"],  # noqa: S607
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(0, result.returncode)
        self.assertEqual(revision, evidence["implementation_snapshot"]["runtime_revision"])

    def test_contract_snapshot_matches_evidence_snapshot(self) -> None:
        evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        expected = evidence["implementation_snapshot"]
        actual = contract["implementation_snapshot"]
        self.assertEqual(expected["revision"], actual["revision"])
        self.assertEqual(expected["runtime_revision"], actual["runtime_revision"])
        self.assertIn("implementation refinement", " ".join(contract["nonclaims"]).lower())


if __name__ == "__main__":
    unittest.main()
