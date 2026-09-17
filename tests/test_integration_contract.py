# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Positive and hostile tests for the three-project integration contract."""

import unittest
from typing import Any, cast

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
    def test_evidence_and_contract_identity_inputs_fail_closed(self) -> None:
        digest = "sha256:" + "a" * 64
        for args, message in (
            (("guidance", "bad", "guidance/evidence/1", digest, 7), "kind"),
            (("guidance", EvidenceKind.GUIDANCE, "../private", digest, 7), "public-safe"),
            (("guidance", EvidenceKind.GUIDANCE, "guidance/evidence/1", "bad", 7), "digest"),
            (("guidance", EvidenceKind.GUIDANCE, "guidance/evidence/1", digest, 0), "positive"),
        ):
            with self.subTest(args=args), self.assertRaisesRegex(IntegrationError, message):
                Evidence(
                    args[0],
                    cast(EvidenceKind, args[1]),
                    args[2],
                    args[3],
                    args[4],
                )

        base = (
            evidence("guidance", EvidenceKind.GUIDANCE),
            evidence("quality", EvidenceKind.QUALITY),
            evidence("quality", EvidenceKind.FORMAL),
        )
        for contract_args, message in (
            (("bad", 7, *base, "coordinator/events/7"), "task id"),
            (("AR-0025", 0, *base, "coordinator/events/7"), "positive"),
            (("AR-0025", 7, *base, "../event"), "public-safe"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(IntegrationError, message):
                IntegrationContract(
                    contract_args[0],
                    contract_args[1],
                    contract_args[2],
                    contract_args[3],
                    contract_args[4],
                    contract_args[5],
                )

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
        for malformed_value in (None, {}, {**contract.as_record(), "extra": True}):
            with self.subTest(malformed=malformed_value):
                self.assertTrue(integration_errors(malformed_value))
        incomplete = contract.as_record()
        incomplete["quality"] = {"project": "quality"}
        self.assertTrue(integration_errors(incomplete))

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
