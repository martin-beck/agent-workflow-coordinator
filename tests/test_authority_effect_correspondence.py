# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Executable bounded correspondence for durable authority-effect outcomes."""

from __future__ import annotations

import unittest
from pathlib import Path

from tools.authority_effect_correspondence import (
    authority_effect_actions,
    validate_authority_effect_model_contract,
)

ROOT = Path(__file__).resolve().parents[1]


class AuthorityEffectCorrespondenceTests(unittest.TestCase):
    def test_model_contract_binds_effect_actions_and_safety_invariants(self) -> None:
        validate_authority_effect_model_contract(ROOT)

    def test_verified_committed_receipt_maps_to_finish_write(self) -> None:
        self.assertEqual(
            ("AcceptWrite", "FinishWrite"),
            authority_effect_actions("committed", receipt_valid=True),
        )

    def test_missing_or_invalid_committed_receipt_is_rejected(self) -> None:
        for receipt_valid in (None, False):
            with (
                self.subTest(receipt_valid=receipt_valid),
                self.assertRaisesRegex(ValueError, "verified receipt"),
            ):
                authority_effect_actions("committed", receipt_valid=receipt_valid)

    def test_ambiguous_outcome_maps_to_write_closed_marking(self) -> None:
        self.assertEqual(
            ("AcceptWrite", "MarkAmbiguous"),
            authority_effect_actions("ambiguous"),
        )

    def test_ambiguous_newer_fence_recovery_maps_to_acquire(self) -> None:
        self.assertEqual(
            ("AcceptWrite", "MarkAmbiguous", "Acquire"),
            authority_effect_actions("ambiguous", recovered_with_new_fence=True),
        )

    def test_invalid_recovery_combinations_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot carry"):
            authority_effect_actions("ambiguous", receipt_valid=False)
        with self.assertRaisesRegex(ValueError, "ambiguous outcome"):
            authority_effect_actions("committed", receipt_valid=True, recovered_with_new_fence=True)
        with self.assertRaisesRegex(ValueError, "invalid"):
            authority_effect_actions("prepared")
