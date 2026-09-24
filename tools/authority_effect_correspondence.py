# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Bounded correspondence checks for durable authority-effect outcomes."""

from __future__ import annotations

from pathlib import Path

MODEL_ACTIONS = frozenset(
    {
        "RequestWrite",
        "FreshRuntimeRead",
        "CompleteReopen",
        "BindForward",
        "ForwardFailure",
        "AcceptWrite",
        "RejectWrite",
        "FinishWrite",
        "MarkAmbiguous",
        "RejectStaleCAS",
        "Acquire",
        "ObserveAuthority",
        "RecheckHeld",
    }
)

MODEL_ACTION_SIGNATURES = {
    "BindForward": "BindForward(p) ==",
    "ForwardFailure": "ForwardFailure(p) ==",
    "FreshRuntimeRead": "FreshRuntimeRead(p) ==",
    "CompleteReopen": "CompleteReopen(p) ==",
    "RequestWrite": "RequestWrite(p) ==",
    "AcceptWrite": "AcceptWrite(p) ==",
    "RejectWrite": "RejectWrite(p) ==",
    "FinishWrite": "FinishWrite(p) ==",
    "MarkAmbiguous": "MarkAmbiguous(p) ==",
    "RejectStaleCAS": "RejectStaleCAS(p, expected) ==",
    "Acquire": "Acquire(p) ==",
    "ObserveAuthority": "ObserveAuthority(p, revision) ==",
    "RecheckHeld": "RecheckHeld(p) ==",
}

MODEL_ACTION_TRANSITIONS = {
    "BindForward": (
        'sessionStatus = "held"',
        "forwardChild = NoChild",
        "forwardChild' = ForwardId(p)",
        "controlRevision' = controlRevision + 1",
    ),
    "ForwardFailure": (
        'sessionStatus = "held"',
        "forwardChild # NoChild",
        "forwardFailed = FALSE",
        "forwardFailed' = TRUE",
    ),
    "FreshRuntimeRead": (
        'sessionStatus = "releasing"',
        "freshRuntimeVerified' = TRUE",
    ),
    "CompleteReopen": (
        'sessionStatus = "releasing"',
        'sessionStatus\' = "released"',
        "controlRevision' = controlRevision + 1",
    ),
    "RequestWrite": (
        'writerPhase[p] = "idle"',
        'sessionStatus # "ambiguous"',
        'writerPhase\' = [writerPhase EXCEPT ![p] = "requested"]',
    ),
    "AcceptWrite": (
        'writerPhase[p] = "requested"',
        'writerPhase\' = [writerPhase EXCEPT ![p] = "accepted"]',
        "writerMutations' = writerMutations + 1",
    ),
    "RejectWrite": (
        'writerPhase[p] = "requested"',
        "sessionStatus \\in UnsafeStatuses",
        'writerPhase\' = [writerPhase EXCEPT ![p] = "rejected"]',
    ),
    "FinishWrite": (
        'writerPhase[p] \\in {"accepted", "rejected"}',
        'writerPhase\' = [writerPhase EXCEPT ![p] = "done"]',
    ),
    "MarkAmbiguous": (
        'sessionStatus \\in {"held", "releasing"}',
        'sessionStatus\' = "ambiguous"',
        "controlRevision' = controlRevision + 1",
    ),
    "RejectStaleCAS": (
        "expected # controlRevision",
        'casResult\' = [casResult EXCEPT ![p] = "rejected"]',
    ),
    "ObserveAuthority": (
        "revision \\in 0..MaxRevision",
        "freshAuthorityRevision' = revision",
    ),
    "RecheckHeld": (
        'sessionStatus = "held"',
        "freshAuthorityRevision = authorityRevision",
        "authorityRechecked' = TRUE",
    ),
    "Acquire": (
        'sessionStatus \\in {"absent", "released"}',
        'sessionStatus\' = "held"',
        "fence' = fence + 1",
        "authorityRevision' = fence + 1",
    ),
}


def validate_authority_effect_model_contract(root: Path) -> None:
    """Require the abstract actions and safety invariants used by this mapper."""
    model = root / "formal/upgrade/HandoffctlUpgradeBarrier.tla"
    try:
        text = model.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError("authority-effect model is unavailable") from error
    missing = [
        f"action {action}"
        for action, signature in MODEL_ACTION_SIGNATURES.items()
        if signature not in text
    ]
    missing.extend(
        f"invariant {invariant}"
        for invariant in ("WriteFence", "AmbiguousIsWriteClosed")
        if f"{invariant} ==" not in text
    )
    if missing:
        raise ValueError(f"authority-effect model contract is incomplete: {', '.join(missing)}")
    transition_missing = [
        f"{action} transition {fragment}"
        for action, fragments in MODEL_ACTION_TRANSITIONS.items()
        for fragment in fragments
        if fragment not in text
    ]
    if transition_missing:
        raise ValueError(
            "authority-effect model transitions are incomplete: " + ", ".join(transition_missing)
        )


def _base_authority_effect_actions(outcome: str, receipt_valid: bool | None) -> tuple[str, ...]:
    """Map the outcome before recovery or stale-fence handling."""
    if outcome == "committed":
        if receipt_valid is not True:
            raise ValueError("committed outcome requires a verified receipt")
        return ("RequestWrite", "AcceptWrite", "FinishWrite")
    if outcome == "ambiguous":
        if receipt_valid is not None:
            raise ValueError("ambiguous outcome cannot carry a receipt verdict")
        return ("RequestWrite", "AcceptWrite", "MarkAmbiguous")
    if outcome == "rejected":
        if receipt_valid is not None:
            raise ValueError("rejected outcome cannot carry a receipt verdict")
        return ("RequestWrite", "RejectWrite")
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
