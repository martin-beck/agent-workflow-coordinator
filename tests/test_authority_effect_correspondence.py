# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Executable bounded correspondence for durable authority-effect outcomes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.authority_effect_correspondence import (
    authority_admission_actions,
    authority_effect_actions,
    validate_authority_effect_model_contract,
)

ROOT = Path(__file__).resolve().parents[1]


class AuthorityEffectCorrespondenceTests(unittest.TestCase):
    def test_model_contract_binds_effect_actions_and_safety_invariants(self) -> None:
        validate_authority_effect_model_contract(ROOT)

    def test_model_contract_rejects_unavailable_or_incomplete_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            with self.assertRaisesRegex(ValueError, "unavailable"):
                validate_authority_effect_model_contract(temporary_root)
            model = temporary_root / "formal/upgrade/HandoffctlUpgradeBarrier.tla"
            model.parent.mkdir(parents=True)
            model.write_text("---- MODULE HandoffctlUpgradeBarrier ----\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "incomplete"):
                validate_authority_effect_model_contract(temporary_root)

    def test_model_contract_requires_admission_action_signatures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            model = temporary_root / "formal/upgrade/HandoffctlUpgradeBarrier.tla"
            model.parent.mkdir(parents=True)
            model.write_text(
                "\n".join(
                    (
                        "AcceptWrite(p) ==",
                        "RejectWrite(p) ==",
                        "FinishWrite(p) ==",
                        "MarkAmbiguous(p) ==",
                        "RejectStaleCAS(p, expected) ==",
                        "Acquire(p) ==",
                        "RecheckHeld(p) ==",
                        "WriteFence ==",
                        "AmbiguousIsWriteClosed ==",
                    )
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "ObserveAuthority"):
                validate_authority_effect_model_contract(temporary_root)

    def test_model_contract_requires_request_transition_semantics(self) -> None:
        model = ROOT / "formal/upgrade/HandoffctlUpgradeBarrier.tla"
        original = model.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            temporary_model = temporary_root / "formal/upgrade/HandoffctlUpgradeBarrier.tla"
            temporary_model.parent.mkdir(parents=True)
            temporary_model.write_text(
                original.replace('writerPhase[p] = "idle"', 'writerPhase[p] = "requested"', 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "RequestWrite transition"):
                validate_authority_effect_model_contract(temporary_root)

    def test_model_contract_requires_reopen_transition_semantics(self) -> None:
        model = ROOT / "formal/upgrade/HandoffctlUpgradeBarrier.tla"
        original = model.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            temporary_model = temporary_root / "formal/upgrade/HandoffctlUpgradeBarrier.tla"
            temporary_model.parent.mkdir(parents=True)
            temporary_model.write_text(
                original.replace(
                    "freshRuntimeVerified' = TRUE", "freshRuntimeVerified' = FALSE", 1
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "FreshRuntimeRead transition"):
                validate_authority_effect_model_contract(temporary_root)

    def test_model_contract_requires_forward_transition_semantics(self) -> None:
        model = ROOT / "formal/upgrade/HandoffctlUpgradeBarrier.tla"
        original = model.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            temporary_model = temporary_root / "formal/upgrade/HandoffctlUpgradeBarrier.tla"
            temporary_model.parent.mkdir(parents=True)
            temporary_model.write_text(
                original.replace("forwardChild' = ForwardId(p)", "forwardChild' = NoChild", 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "BindForward transition"):
                validate_authority_effect_model_contract(temporary_root)

    def test_model_contract_requires_effect_transition_semantics(self) -> None:
        model = ROOT / "formal/upgrade/HandoffctlUpgradeBarrier.tla"
        original = model.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            temporary_model = temporary_root / "formal/upgrade/HandoffctlUpgradeBarrier.tla"
            temporary_model.parent.mkdir(parents=True)
            temporary_model.write_text(
                original.replace('sessionStatus\' = "ambiguous"', 'sessionStatus\' = "held"', 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "MarkAmbiguous transition"):
                validate_authority_effect_model_contract(temporary_root)

    def test_model_contract_requires_admission_transition_semantics(self) -> None:
        model = ROOT / "formal/upgrade/HandoffctlUpgradeBarrier.tla"
        original = model.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            temporary_model = temporary_root / "formal/upgrade/HandoffctlUpgradeBarrier.tla"
            temporary_model.parent.mkdir(parents=True)
            temporary_model.write_text(
                original.replace(
                    "freshAuthorityRevision' = revision", "freshAuthorityRevision' = 0", 1
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "ObserveAuthority transition"):
                validate_authority_effect_model_contract(temporary_root)

    def test_model_contract_binds_effect_transition_semantics(self) -> None:
        validate_authority_effect_model_contract(ROOT)

    def test_verified_committed_receipt_maps_to_finish_write(self) -> None:
        self.assertEqual(
            ("RequestWrite", "AcceptWrite", "FinishWrite"),
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
            ("RequestWrite", "AcceptWrite", "MarkAmbiguous"),
            authority_effect_actions("ambiguous"),
        )

    def test_ambiguous_newer_fence_recovery_maps_to_acquire(self) -> None:
        self.assertEqual(
            ("RequestWrite", "AcceptWrite", "MarkAmbiguous", "Acquire"),
            authority_effect_actions("ambiguous", recovered_with_new_fence=True),
        )

    def test_rejected_effect_maps_to_reject_write(self) -> None:
        self.assertEqual(("RequestWrite", "RejectWrite"), authority_effect_actions("rejected"))

    def test_rejected_receipt_verdict_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "receipt verdict"):
            authority_effect_actions("rejected", receipt_valid=False)

    def test_stale_fence_rejection_maps_before_new_fence_recovery(self) -> None:
        self.assertEqual(
            ("RequestWrite", "AcceptWrite", "MarkAmbiguous", "RejectStaleCAS", "Acquire"),
            authority_effect_actions(
                "ambiguous", stale_fence_rejected=True, recovered_with_new_fence=True
            ),
        )

    def test_stale_fence_rejection_requires_ambiguous_outcome(self) -> None:
        with self.assertRaisesRegex(ValueError, "stale-fence rejection"):
            authority_effect_actions("committed", receipt_valid=True, stale_fence_rejected=True)

    def test_rechecked_authority_admission_maps_to_model_reread(self) -> None:
        self.assertEqual(
            ("ObserveAuthority", "RecheckHeld"),
            authority_admission_actions(authority_rechecked=True, admission_allowed=True),
        )

    def test_rechecked_stale_authority_admission_maps_to_rejection(self) -> None:
        self.assertEqual(
            ("ObserveAuthority", "RecheckHeld", "RejectStaleCAS"),
            authority_admission_actions(authority_rechecked=True, admission_allowed=False),
        )

    def test_authority_admission_without_reread_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "trusted reread"):
            authority_admission_actions(authority_rechecked=False, admission_allowed=True)

    def test_invalid_recovery_combinations_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot carry"):
            authority_effect_actions("ambiguous", receipt_valid=False)
        with self.assertRaisesRegex(ValueError, "ambiguous outcome"):
            authority_effect_actions("committed", receipt_valid=True, recovered_with_new_fence=True)
        with self.assertRaisesRegex(ValueError, "ambiguous outcome"):
            authority_effect_actions("rejected", stale_fence_rejected=True)
        with self.assertRaisesRegex(ValueError, "invalid"):
            authority_effect_actions("prepared")
