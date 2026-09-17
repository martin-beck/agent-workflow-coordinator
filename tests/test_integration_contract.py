# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Positive and hostile tests for the three-project integration contract."""

import unittest
from typing import Any

from tools.integration_contract import (
    Evidence,
    EvidenceKind,
    IntegrationContract,
    IntegrationError,
    integration_errors,
)


def evidence(project: str, kind: EvidenceKind) -> Evidence:
    return Evidence(project, kind, f"{project}/evidence/1", "sha256:" + "a" * 64, 7)


class IntegrationContractTests(unittest.TestCase):
    def test_complete_contract_is_digestable_and_has_single_authority(self) -> None:
        contract = IntegrationContract(
            "AR-0025",
            7,
            evidence("guidance", EvidenceKind.GUIDANCE),
            evidence("quality", EvidenceKind.QUALITY),
            evidence("quality", EvidenceKind.FORMAL),
            "coordinator/events/7",
        )
        self.assertTrue(contract.digest.startswith("sha256:"))
        self.assertEqual("coordinator", contract.as_record()["decision_authority"])

    def test_rejects_revision_drift_and_non_coordinator_authority(self) -> None:
        with self.assertRaisesRegex(IntegrationError, "revision"):
            IntegrationContract(
                "AR-0025",
                7,
                evidence("guidance", EvidenceKind.GUIDANCE),
                evidence("quality", EvidenceKind.QUALITY),
                Evidence("quality", EvidenceKind.FORMAL, "quality/f/1", "sha256:" + "b" * 64, 8),
                "coordinator/events/7",
            )
        with self.assertRaisesRegex(IntegrationError, "only Coordinator"):
            IntegrationContract(
                "AR-0025",
                7,
                evidence("guidance", EvidenceKind.GUIDANCE),
                evidence("quality", EvidenceKind.QUALITY),
                evidence("quality", EvidenceKind.FORMAL),
                "coordinator/events/7",
                "guidance",
            )

    def test_serialized_contract_fails_closed(self) -> None:
        contract = IntegrationContract(
            "AR-0025",
            7,
            evidence("guidance", EvidenceKind.GUIDANCE),
            evidence("quality", EvidenceKind.QUALITY),
            evidence("quality", EvidenceKind.FORMAL),
            "coordinator/events/7",
        )
        self.assertEqual([], integration_errors(contract.as_record()))
        malformed: dict[str, Any] = dict(contract.as_record())
        malformed["guidance"] = dict(malformed["guidance"], digest="not-a-digest")
        self.assertTrue(integration_errors(malformed))

    def test_projects_cannot_misrepresent_each_others_evidence(self) -> None:
        with self.assertRaisesRegex(IntegrationError, "guidance evidence kind"):
            evidence("guidance", EvidenceKind.QUALITY)
        with self.assertRaisesRegex(IntegrationError, "quality evidence kind"):
            evidence("quality", EvidenceKind.GUIDANCE)
        with self.assertRaisesRegex(IntegrationError, "project"):
            Evidence(
                "coordinator",
                EvidenceKind.FORMAL,
                "coordinator/evidence/1",
                "sha256:" + "a" * 64,
                7,
            )


if __name__ == "__main__":
    unittest.main()
