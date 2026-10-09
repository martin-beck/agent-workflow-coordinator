# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Regression checks for the bounded opt-in receipt contract."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORMAL = ROOT / "formal" / "handoffctl"


def test_receipt_model_keeps_local_remote_and_ambiguous_outcomes_distinct() -> None:
    model = (FORMAL / "HandoffctlReceipts.tla").read_text(encoding="utf-8")
    assert 'phase[r] = "queued" => ~committed[r] /\\ ~remoteObserved[r]' in model
    assert 'phase[r] = "published_remote" => remoteObserved[r]' in model
    assert 'phase[r] = "ambiguous" => ~remoteObserved[r]' in model
    assert "RecoverCommitted(r)" in model
    assert "MarkAmbiguous(r)" in model


def test_receipt_model_is_a_fast_safety_and_full_liveness_gate() -> None:
    verifier = (FORMAL / "verify.sh").read_text(encoding="utf-8")
    tiers = (ROOT / "formal" / "tier-evidence.json").read_text(encoding="utf-8")
    fast = (FORMAL / "HandoffctlReceiptsFast.cfg").read_text(encoding="utf-8")
    full = (FORMAL / "HandoffctlReceipts.cfg").read_text(encoding="utf-8")
    assert "run_model HandoffctlReceiptsFast HandoffctlReceipts" in verifier
    assert "run_model HandoffctlReceipts" in verifier
    assert '"HandoffctlReceiptsFast"' in tiers
    assert '"HandoffctlReceipts"' in tiers
    assert "PROPERTIES" not in fast
    assert "ProgressAfterServicesStabilize" in full
