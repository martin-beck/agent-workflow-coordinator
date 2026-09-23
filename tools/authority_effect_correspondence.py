# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Bounded correspondence checks for durable authority-effect outcomes."""

from __future__ import annotations

from pathlib import Path

MODEL_ACTIONS = frozenset({"AcceptWrite", "RejectWrite", "FinishWrite", "MarkAmbiguous", "Acquire"})


def validate_authority_effect_model_contract(root: Path) -> None:
    """Require the abstract actions and safety invariants used by this mapper."""
    model = root / "formal/upgrade/HandoffctlUpgradeBarrier.tla"
    try:
        text = model.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError("authority-effect model is unavailable") from error
    missing = [f"action {action}" for action in MODEL_ACTIONS if f"{action}(p) ==" not in text]
    missing.extend(
        f"invariant {invariant}"
        for invariant in ("WriteFence", "AmbiguousIsWriteClosed")
        if f"{invariant} ==" not in text
    )
    if missing:
        raise ValueError(f"authority-effect model contract is incomplete: {', '.join(missing)}")


def authority_effect_actions(
    outcome: str,
    *,
    receipt_valid: bool | None = None,
    recovered_with_new_fence: bool = False,
) -> tuple[str, ...]:
    """Map one bounded concrete effect outcome to abstract model actions.

    This is deliberately diagnostic: it classifies executable evidence and
    never authorizes a concrete authority effect.
    """
    actions: tuple[str, ...]
    if outcome == "committed":
        if receipt_valid is not True:
            raise ValueError("committed outcome requires a verified receipt")
        actions = ("AcceptWrite", "FinishWrite")
    elif outcome == "ambiguous":
        if receipt_valid is not None:
            raise ValueError("ambiguous outcome cannot carry a receipt verdict")
        actions = ("AcceptWrite", "MarkAmbiguous")
    else:
        raise ValueError("authority-effect outcome is invalid")
    if recovered_with_new_fence:
        if outcome != "ambiguous":
            raise ValueError("new-fence recovery requires an ambiguous outcome")
        actions = (*actions, "Acquire")
    return actions
