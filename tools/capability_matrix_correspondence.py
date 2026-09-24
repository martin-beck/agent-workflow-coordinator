# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Hostile source contract for the bounded capability matrix model."""

from __future__ import annotations

from pathlib import Path

MODEL = Path("formal/roles/CapabilityMatrix.tla")
REQUIRED_FRAGMENTS = {
    "TypeOK": (
        r"assignments \in [Owners -> SUBSET Roles]",
        r"executor \in [Tasks -> (Owners \cup {NoOwner})]",
        r"reviewer \in [Tasks -> (Owners \cup {NoReviewer})]",
    ),
    "NoMutationWithoutRoleAuthorization": (
        r'taskStatus[t] = "executed" =>',
        r"executor[t] # NoOwner",
        r"taskAction[t] \in MutationActions",
        r"ImplementerRole \in assignments[executor[t]]",
    ),
    "ReviewerDistinctFromExecutor": (r"reviewer[t] # NoReviewer => reviewer[t] # executor[t]",),
    "SecurityTaskRequiresSecurityRole": (
        r't \in SecurityTasks /\ taskStatus[t] = "executed" =>',
        r"SecurityRole \in assignments[executor[t]]",
    ),
    "DoneRequiresSpecAcceptance": (
        r'taskStatus[t] = "done" =>',
        r"specResolved[t] /\ acceptancePassed[t]",
    ),
}


def validate_capability_model(root: Path) -> None:
    """Require invariant declarations and safety bodies in the model."""
    try:
        text = (root / MODEL).read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError("capability model is unavailable") from error
    missing = [f"invariant {name}" for name in REQUIRED_FRAGMENTS if f"{name} ==" not in text]
    missing.extend(
        f"invariant {name} semantics {fragment}"
        for name, fragments in REQUIRED_FRAGMENTS.items()
        for fragment in fragments
        if fragment not in text
    )
    if missing:
        raise ValueError("capability model contract is incomplete: " + ", ".join(missing))
