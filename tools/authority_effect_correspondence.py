# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Bounded correspondence checks for durable authority-effect outcomes."""

from __future__ import annotations

from pathlib import Path

MODEL_ACTIONS = frozenset(
    {
        "AcceptWrite",
        "RejectWrite",
        "FinishWrite",
        "MarkAmbiguous",
        "RejectStaleCAS",
        "Acquire",
    }
)


def validate_authority_effect_model_contract(root: Path) -> None:
    """Require the abstract actions and safety invariants used by this mapper."""
    model = root / "formal/upgrade/HandoffctlUpgradeBarrier.tla"
    try:
        text = model.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError("authority-effect model is unavailable") from error
    signatures = {
        action: f"{action}(p) ==" for action in MODEL_ACTIONS if action != "RejectStaleCAS"
    }
    signatures["RejectStaleCAS"] = "RejectStaleCAS(p, expected) =="
    missing = [
        f"action {action}" for action, signature in signatures.items() if signature not in text
    ]
    missing.extend(
        f"invariant {invariant}"
        for invariant in ("WriteFence", "AmbiguousIsWriteClosed")
        if f"{invariant} ==" not in text
    )
    if missing:
        raise ValueError(f"authority-effect model contract is incomplete: {', '.join(missing)}")


def _base_authority_effect_actions(outcome: str, receipt_valid: bool | None) -> tuple[str, ...]:
    """Map the outcome before recovery or stale-fence handling."""
    if outcome == "committed":
        if receipt_valid is not True:
            raise ValueError("committed outcome requires a verified receipt")
        return ("AcceptWrite", "FinishWrite")
    if outcome == "ambiguous":
        if receipt_valid is not None:
            raise ValueError("ambiguous outcome cannot carry a receipt verdict")
        return ("AcceptWrite", "MarkAmbiguous")
    if outcome == "rejected":
        if receipt_valid is not None:
            raise ValueError("rejected outcome cannot carry a receipt verdict")
        return ("RejectWrite",)
    raise ValueError("authority-effect outcome is invalid")


def _apply_recovery_actions(
    outcome: str,
    actions: tuple[str, ...],
    *,
    recovered_with_new_fence: bool,
    stale_fence_rejected: bool,
) -> tuple[str, ...]:
    """Append only recovery actions justified by an ambiguous outcome."""
    if outcome == "rejected" and (recovered_with_new_fence or stale_fence_rejected):
        raise ValueError("recovery flags require an ambiguous outcome")
    if stale_fence_rejected:
        if outcome != "ambiguous":
            raise ValueError("stale-fence rejection requires an ambiguous outcome")
        actions = (*actions, "RejectStaleCAS")
    if recovered_with_new_fence:
        if outcome != "ambiguous":
            raise ValueError("new-fence recovery requires an ambiguous outcome")
        actions = (*actions, "Acquire")
    return actions


def authority_effect_actions(
    outcome: str,
    *,
    receipt_valid: bool | None = None,
    recovered_with_new_fence: bool = False,
    stale_fence_rejected: bool = False,
) -> tuple[str, ...]:
    """Map one bounded concrete effect outcome to abstract model actions.

    This is deliberately diagnostic: it classifies executable evidence and
    never authorizes a concrete authority effect.
    """
    return _apply_recovery_actions(
        outcome,
        _base_authority_effect_actions(outcome, receipt_valid),
        recovered_with_new_fence=recovered_with_new_fence,
        stale_fence_rejected=stale_fence_rejected,
    )


def authority_admission_actions(
    *, authority_rechecked: bool, admission_allowed: bool
) -> tuple[str, ...]:
    """Classify a bound authority reread and its non-mutating admission result."""
    if not authority_rechecked:
        raise ValueError("authority admission requires a trusted reread")
    if admission_allowed:
        return ("ObserveAuthority", "RecheckHeld")
    return ("ObserveAuthority", "RecheckHeld", "RejectStaleCAS")
