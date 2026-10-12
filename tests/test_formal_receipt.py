# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Deterministic exact-head validation for bounded formal receipts."""

from __future__ import annotations

import copy
import unittest
from pathlib import Path

from tools.formal_receipt import (
    FormalReceiptError,
    expected_candidate,
    expected_contract,
    validate_receipt,
)

ROOT = Path(__file__).resolve().parent.parent
TIER = "pr-publication"


def receipt() -> dict[str, object]:
    contract = expected_contract(ROOT, TIER)
    candidate = expected_candidate(ROOT)
    return {
        "status": "success",
        "profile": TIER,
        "commit": candidate,
        "candidate_identity": {"kind": "source", "commit": candidate},
        "models": contract["models"],
        "configs": contract["configs"],
        "outcomes": dict.fromkeys(contract["models"], "success"),
        "verifier_budget": {"declared_seconds": contract["budget_seconds"], "elapsed_seconds": 1},
    }


class FormalReceiptTests(unittest.TestCase):
    def test_accepts_exact_head_and_declared_bounded_contract(self) -> None:
        validate_receipt(receipt(), root=ROOT, candidate=expected_candidate(ROOT), tier=TIER)

    def test_rejects_stale_candidate_fixture(self) -> None:
        value = receipt()
        value["commit"] = "a" * 40
        value["candidate_identity"] = {"kind": "source", "commit": "a" * 40}
        with self.assertRaisesRegex(FormalReceiptError, "stale"):
            validate_receipt(value, root=ROOT, candidate=expected_candidate(ROOT), tier=TIER)

    def test_rejects_resource_exhaustion_fixture(self) -> None:
        value = copy.deepcopy(receipt())
        outcomes = value["outcomes"]
        assert isinstance(outcomes, dict)
        outcomes["HandoffctlStorage"] = "storage-exhausted"
        with self.assertRaisesRegex(FormalReceiptError, "resource exhaustion"):
            validate_receipt(value, root=ROOT, candidate=expected_candidate(ROOT), tier=TIER)

    def test_rejects_timeout_fixture(self) -> None:
        value = receipt()
        value["status"] = "timeout"
        with self.assertRaisesRegex(FormalReceiptError, "timeout"):
            validate_receipt(value, root=ROOT, candidate=expected_candidate(ROOT), tier=TIER)

    def test_rejects_budget_and_model_configuration_drift(self) -> None:
        value = receipt()
        budget = value["verifier_budget"]
        assert isinstance(budget, dict)
        budget["elapsed_seconds"] = 901
        with self.assertRaisesRegex(FormalReceiptError, "budget"):
            validate_receipt(value, root=ROOT, candidate=expected_candidate(ROOT), tier=TIER)
        value = receipt()
        models = value["models"]
        assert isinstance(models, dict)
        models["HandoffctlLiveness"] = "0" * 64
        with self.assertRaisesRegex(FormalReceiptError, "model digest"):
            validate_receipt(value, root=ROOT, candidate=expected_candidate(ROOT), tier=TIER)


if __name__ == "__main__":
    unittest.main()
