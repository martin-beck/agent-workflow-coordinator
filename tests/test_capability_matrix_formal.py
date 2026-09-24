# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Hostile contract tests for the capability matrix model."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.capability_matrix_correspondence import validate_capability_model

ROOT = Path(__file__).resolve().parents[1]


class CapabilityMatrixFormalTests(unittest.TestCase):
    def test_evidence_map_is_explicit_about_bounded_status_and_next_consumer(self) -> None:
        evidence = json.loads((ROOT / "formal/roles/evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(evidence["schema_version"], 1)
        self.assertEqual(evidence["kind"], "bounded-capability-matrix-formal-evidence")
        self.assertEqual(evidence["evidence_status"], "bounded-model-and-hostile-contract")
        self.assertEqual(evidence["correspondence_claim"], "not-proven")
        self.assertEqual(evidence["runtime_admission"]["status"], "pending")
        self.assertEqual(evidence["runtime_admission"]["next_consumer"], "AR-0073")
        for relative_path in [
            evidence["model"],
            evidence["config"],
            *evidence["implementation_tests"],
        ]:
            self.assertTrue((ROOT / relative_path).is_file(), relative_path)

    def test_model_binds_authorization_review_and_security_invariants(self) -> None:
        validate_capability_model(ROOT)

    def test_model_missing_is_rejected(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(ValueError, "capability model is unavailable"),
        ):
            validate_capability_model(Path(directory))

    def test_model_rejects_weakened_mutation_authorization(self) -> None:
        original = (ROOT / "formal/roles/CapabilityMatrix.tla").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            model = temporary_root / "formal/roles/CapabilityMatrix.tla"
            model.parent.mkdir(parents=True)
            model.write_text(
                original.replace(
                    r"ImplementerRole \in assignments[executor[t]]",
                    "TRUE",
                    2,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "NoMutationWithoutRoleAuthorization semantics"):
                validate_capability_model(temporary_root)

    def test_model_rejects_weakened_reviewer_distinction(self) -> None:
        original = (ROOT / "formal/roles/CapabilityMatrix.tla").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            model = temporary_root / "formal/roles/CapabilityMatrix.tla"
            model.parent.mkdir(parents=True)
            model.write_text(
                original.replace(
                    r"reviewer[t] # NoReviewer => reviewer[t] # executor[t]",
                    "TRUE",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "ReviewerDistinctFromExecutor semantics"):
                validate_capability_model(temporary_root)
