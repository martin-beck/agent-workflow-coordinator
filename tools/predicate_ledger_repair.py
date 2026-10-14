# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Audited repair of one predicate-evidence row after task inactivity."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .task_spec import EVIDENCE_REF, EvidencePolicy, evidence_ledger_errors

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_REASON = 512


def _digest(root: Path, reference: str) -> str | None:
    """Hash one bounded, relative regular file without following symlinks."""
    path = Path(reference)
    if path.is_absolute() or ".." in path.parts:
        return None
    target = root / path
    try:
        if target.is_symlink() or not target.is_file() or target.stat().st_size > 4 * 1024 * 1024:
            return None
        return "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        return None


def repair(  # noqa: C901
    root: Path, args: Any, meta: dict[str, Any], policy: EvidencePolicy | None
) -> str:
    """Append a fresh pass while retaining every previous ledger row."""
    if args.expected_revision != meta.get("task_revision"):
        raise RuntimeError(
            "stale revision: expected "
            f"{args.expected_revision}, current {meta.get('task_revision')}"
        )
    if meta.get("status") not in {"open", "done"} or meta.get("owner") or meta.get("claim_expires"):
        raise RuntimeError("predicate-ledger repair requires an inactive, unowned task")
    reason = str(args.reason)
    if not reason.strip() or len(reason) > _MAX_REASON or any(ord(c) < 32 for c in reason):
        raise RuntimeError("predicate-ledger repair reason is empty or unbounded")
    predicate = str(args.predicate)
    evidence_ref = str(args.evidence_ref)
    evidence_digest = str(args.evidence_digest)
    if (
        not EVIDENCE_REF.fullmatch(evidence_ref)
        or evidence_ref.startswith("/")
        or ".." in Path(evidence_ref).parts
    ):
        raise RuntimeError("predicate-ledger repair evidence ref is unsafe")
    if not _DIGEST.fullmatch(evidence_digest) or _digest(root, evidence_ref) != evidence_digest:
        raise RuntimeError("predicate-ledger repair evidence digest is not fresh")
    ledger = meta.get("evidence_ledger")
    if not isinstance(ledger, dict) or set(ledger) != {
        "candidate_ref",
        "candidate_digest",
        "steps",
    }:
        raise RuntimeError("predicate-ledger repair requires a canonical evidence ledger")
    steps = ledger.get("steps")
    if not isinstance(steps, list):
        raise RuntimeError("predicate-ledger repair requires a canonical evidence ledger")
    # Validate the task spec and predicate through the normal ledger validator.
    candidate_ref = ledger.get("candidate_ref")
    candidate_digest = ledger.get("candidate_digest")
    fresh_candidate_ref = getattr(args, "candidate_ref", None)
    fresh_candidate_digest = getattr(args, "candidate_digest", None)
    if fresh_candidate_ref is not None or fresh_candidate_digest is not None:
        if (
            not isinstance(fresh_candidate_ref, str)
            or not _DIGEST.fullmatch(str(fresh_candidate_digest))
            or _digest(root, fresh_candidate_ref) != fresh_candidate_digest
        ):
            raise RuntimeError("predicate-ledger repair candidate is stale")
        candidate_ref, candidate_digest = fresh_candidate_ref, fresh_candidate_digest
    elif not isinstance(candidate_ref, str) or _digest(root, candidate_ref) != candidate_digest:
        raise RuntimeError("predicate-ledger repair candidate is stale")
    fresh = {
        "predicate_id": predicate,
        "status": "pass",
        "candidate_ref": candidate_ref,
        "candidate_digest": candidate_digest,
        "evidence_class": args.evidence_class,
        "evidence_ref": evidence_ref,
        "evidence_digest": evidence_digest,
        "recorded_task_revision": int(meta["task_revision"]) + 1,
    }
    prospective = dict(ledger, candidate_ref=candidate_ref, candidate_digest=candidate_digest)
    historical = []
    for row in steps:
        if not isinstance(row, dict):
            raise RuntimeError("predicate-ledger repair found a malformed historical row")
        retained = dict(row)
        if retained.get("predicate_id") == predicate and retained.get("status") == "pass":
            retained["status"] = "stale"
        historical.append(retained)
    prospective["steps"] = [*historical, fresh]
    prospective_meta = dict(meta, evidence_ledger=prospective)
    errors = evidence_ledger_errors(root, prospective_meta, policy)
    if errors:
        raise RuntimeError("predicate-ledger repair evidence is invalid: " + "; ".join(errors))
    meta["evidence_ledger"] = prospective
    return reason
