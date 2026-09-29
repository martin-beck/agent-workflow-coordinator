# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Compose admitted production effects with generated phase operations.

This is an internal capability factory.  It consumes already-issued admission
bundles and backend-owned effect arguments; it does not resolve live state,
create admission evidence, or connect the public upgrade dispatcher.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from tools.authority_neutral_commit import CommitAdmissionBundle
from tools.authority_neutral_commit_dispatch import (
    BoundCommitPhaseAdapter,
    DurableCommitExecutor,
)
from tools.authority_neutral_rollback_dispatch import (
    BoundRollbackPhaseAdapter,
    DurableRollbackExecutor,
)
from tools.production_effect_binding import (
    bind_durable_commit_capability,
    bind_durable_rollback_capability,
)
from tools.production_phase_engine import ProductionPhaseBinding


class ProductionPhaseEffectError(ValueError):
    """Generated operations and admitted effects cannot be composed safely."""


@dataclass(frozen=True, slots=True)
class ProductionPhaseEffects:
    """The single-use internal commit and rollback phase capabilities."""

    commit: BoundCommitPhaseAdapter
    rollback: BoundRollbackPhaseAdapter


def _operation_inputs(operation: Mapping[str, object], *, target: str) -> dict[str, object]:
    inputs = operation.get("inputs")
    if not isinstance(inputs, Mapping):
        raise ProductionPhaseEffectError("phase operation inputs are invalid")
    required = {
        "backend",
        "selector_ref",
        "expected_state_revision",
        "barrier_id",
        "fencing_token",
        "backup_operation_id",
    }
    if target == "rollback":
        required.add("target")
    if set(inputs) != required:
        raise ProductionPhaseEffectError("phase operation inputs are incomplete")
    if inputs.get("target", target) != target:
        raise ProductionPhaseEffectError("phase operation target is invalid")
    if inputs.get("backend") not in {"git", "sqlite"}:
        raise ProductionPhaseEffectError("phase operation backend is invalid")
    if type(inputs.get("expected_state_revision")) is not int:
        raise ProductionPhaseEffectError("phase operation state revision is invalid")
    for field in ("selector_ref", "barrier_id", "fencing_token"):
        if not isinstance(inputs.get(field), str) or not inputs[field]:
            raise ProductionPhaseEffectError(f"phase operation {field} is invalid")
    return dict(inputs)


def _validate_admission(
    operation: Mapping[str, object],
    admission: CommitAdmissionBundle,
    *,
    target: str,
    suffix: str,
) -> dict[str, object]:
    operation_id = operation.get("operation_id")
    if not isinstance(operation_id, str) or operation_id != (
        f"{operation_id.rsplit(':', 1)[0]}:{suffix}"
    ):
        raise ProductionPhaseEffectError("phase operation identity is invalid")
    inputs = _operation_inputs(operation, target=target)
    if not isinstance(admission, CommitAdmissionBundle):
        raise ProductionPhaseEffectError("phase effect admission is invalid")
    expected = {
        "backend": inputs["backend"],
        "target": target,
        "operation_id": operation_id,
        "state_revision": inputs["expected_state_revision"],
        "barrier_id": inputs["barrier_id"],
        "fencing_token": inputs["fencing_token"],
    }
    actual = {
        "backend": admission.backend,
        "target": admission.target,
        "operation_id": admission.operation_id,
        "state_revision": admission.state_revision,
        "barrier_id": admission.barrier_id,
        "fencing_token": admission.fencing_token,
    }
    if actual != expected:
        raise ProductionPhaseEffectError("phase admission does not match operation identity")
    return inputs


def _adapter_context(
    operation: Mapping[str, object], inputs: Mapping[str, object], *, target: str
) -> dict[str, object]:
    operation_id = operation.get("operation_id")
    if not isinstance(operation_id, str):  # pragma: no cover - admission validation precedes this
        raise ProductionPhaseEffectError("phase operation identity is invalid")
    return {
        "backend": inputs["backend"],
        "target": target,
        "selector_ref": inputs["selector_ref"],
        "operation_id": operation_id,
        "state_revision": inputs["expected_state_revision"],
        "durable_barrier_id": inputs["barrier_id"],
        "fencing_token": inputs["fencing_token"],
    }


def bind_production_phase_effects(
    binding: ProductionPhaseBinding,
    journal: object,
    commit_admission: CommitAdmissionBundle,
    rollback_admission: CommitAdmissionBundle,
    *,
    session_revision: int,
    admission_reread: Callable[[], Mapping[str, object]],
    commit_argument: object,
    rollback_argument: object,
    rollback_effect: Callable[[object], object],
    runner: Any | None = None,
    connector: Any | None = None,
) -> ProductionPhaseEffects:
    """Bind exact generated commit/rollback operations to durable effects.

    The caller owns admission and backend evidence.  This function only
    composes those already-bound capabilities with the generated operations;
    public command dispatch remains unaffected.
    """
    if type(binding) is not ProductionPhaseBinding:
        raise ProductionPhaseEffectError("production phase binding is invalid")
    if not callable(admission_reread) or not callable(rollback_effect):
        raise ProductionPhaseEffectError("phase effect callbacks are invalid")
    commit_operation = binding.operations.get("commit")
    rollback_operation = binding.operations.get("rollback")
    if not isinstance(commit_operation, Mapping) or not isinstance(rollback_operation, Mapping):
        raise ProductionPhaseEffectError("generated commit and rollback operations are required")
    commit_inputs = _validate_admission(
        commit_operation, commit_admission, target="new", suffix="commit"
    )
    rollback_inputs = _validate_admission(
        rollback_operation, rollback_admission, target="rollback", suffix="rollback"
    )
    try:
        commit_capability = bind_durable_commit_capability(
            binding=binding.live_binding,
            admission=commit_admission,
            journal=journal,
            session_revision=session_revision,
            admission_reread=admission_reread,
            runner=runner,
            connector=connector,
        )
        rollback_capability = bind_durable_rollback_capability(
            binding=binding.live_binding,
            admission=rollback_admission,
            journal=journal,
            rollback_effect=rollback_effect,
            session_revision=session_revision,
        )
        commit_adapter = BoundCommitPhaseAdapter(
            commit_operation,
            _adapter_context(commit_operation, commit_inputs, target="new"),
            cast(DurableCommitExecutor, commit_capability),
            commit_argument,
        )
        rollback_adapter = BoundRollbackPhaseAdapter(
            rollback_operation,
            _adapter_context(rollback_operation, rollback_inputs, target="rollback"),
            cast(DurableRollbackExecutor, rollback_capability),
            rollback_argument,
        )
    except ProductionPhaseEffectError:
        raise
    except Exception as error:
        raise ProductionPhaseEffectError("production phase effects were rejected") from error
    return ProductionPhaseEffects(commit_adapter, rollback_adapter)
