# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Fail-closed decision artifact for upgrade mutation enablement.

The formal model is best-effort design guidance. Mutation admission is based on
concrete operational evidence, not on a Python-to-TLA+ refinement proof.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class MutationGateError(ValueError):
    """Raised when a refinement contract cannot produce a safe decision."""


def evaluate_mutation_gate(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deterministic mutation decision; never enable on partial evidence."""
    if not isinstance(contract, Mapping):
        raise MutationGateError("mutation contract is not an object")
    boundary = contract.get("refinement_boundary")
    if not isinstance(boundary, Mapping):
        raise MutationGateError("mutation contract refinement boundary is missing")
    reasons: list[str] = []
    if boundary.get("implementation_refinement") != "not-required":
        reasons.append("refinement_proof_requirement_not_removed")
    correspondence = contract.get("correspondence")
    if not isinstance(correspondence, list) or not correspondence:
        raise MutationGateError("mutation contract correspondence is missing")
    for index, entry in enumerate(correspondence):
        if not isinstance(entry, Mapping):
            raise MutationGateError(f"mutation correspondence entry {index} is invalid")
        if entry.get("status") != "evidence-complete":
            reasons.append(f"operational_obligation_{index}_incomplete")
    if "rejection-only" not in str(
        contract.get("mutation_gate", "")
    ) or "exact-head executable evidence" not in str(contract.get("mutation_gate", "")):
        reasons.append("mutation_gate_policy_not_enabled")
    return {
        "mutation_enabled": not reasons,
        "decision": "deny" if reasons else "allow",
        "reasons": reasons,
        "implementation_refinement": boundary.get("implementation_refinement"),
        "model_role": "best-effort-design-guidance",
        "authority_mutation_dispatched": False,
    }
