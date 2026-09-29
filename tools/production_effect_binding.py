# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Bind concrete durable authority effects to an admitted live session.

The returned capabilities are internal, single-use effect seams.  This module
does not expose them through the public upgrade command and never manufactures
admission evidence or a reread callback.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from tools.authority_neutral_commit import CommitAdmissionBundle
from tools.git_authority_adapter import GitAuthorityAdapter
from tools.sqlite_authority_adapter import SQLiteAuthorityAdapter
from tools.upgrade_binding import LiveUpgradeBinding


class ProductionEffectBindingError(ValueError):
    """A durable backend effect cannot be bound to the live identity."""


def _validate_common(
    binding: LiveUpgradeBinding,
    admission: CommitAdmissionBundle,
    session_revision: int,
    admission_reread: object,
    *,
    expected_target: str,
    require_reread: bool = True,
) -> None:
    if type(binding) is not LiveUpgradeBinding or not binding.is_admitted():
        raise ProductionEffectBindingError("an admitted live upgrade binding is required")
    if not isinstance(admission, CommitAdmissionBundle):
        raise ProductionEffectBindingError("a typed commit admission bundle is required")
    if admission.target != expected_target:
        raise ProductionEffectBindingError(f"{expected_target} effect admission is required")
    if type(session_revision) is not int or session_revision < 1:
        raise ProductionEffectBindingError("effect session revision is invalid")
    envelope = binding.runtime.runtime_envelope
    expected = {
        "backend": admission.backend,
        "operation_id": admission.operation_id.rsplit(":", 1)[0],
        "state_revision": admission.state_revision,
        "durable_barrier_id": admission.barrier_id,
        "fencing_token": admission.fencing_token,
    }
    if any(envelope.get(field) != value for field, value in expected.items()):
        raise ProductionEffectBindingError("effect admission does not match live identity")
    if session_revision != admission.state_revision:
        raise ProductionEffectBindingError("effect session revision does not match admission")
    if require_reread and not callable(admission_reread):
        raise ProductionEffectBindingError("effect admission reread is required")


def bind_durable_commit_capability(
    binding: LiveUpgradeBinding,
    admission: CommitAdmissionBundle,
    journal: object,
    *,
    session_revision: int,
    admission_reread: Callable[[], Mapping[str, object]],
    runner: Any | None = None,
    connector: Any | None = None,
) -> object:
    """Bind one concrete Git or SQLite effect to the durable journal.

    The caller must provide a live admission reread.  A callback derived from
    stale or synthetic data is not accepted by this factory's contract.
    """
    _validate_common(
        binding,
        admission,
        session_revision,
        admission_reread,
        expected_target="new",
    )
    adapter = cast(Any, binding.adapter)
    try:
        if type(adapter) is GitAuthorityAdapter:
            if not binding.expected_branch or not binding.expected_head:
                raise ProductionEffectBindingError("Git effect identity is incomplete")
            kwargs: dict[str, object] = {
                "session_revision": session_revision,
                "admission_reread": admission_reread,
                "expected_branch": binding.expected_branch,
                "expected_head": binding.expected_head,
            }
            if runner is not None:
                kwargs["runner"] = runner
            return cast(Any, adapter).bind_durable_commit_capability(admission, journal, **kwargs)
        if type(adapter) is SQLiteAuthorityAdapter:
            kwargs = {
                "session_revision": session_revision,
                "admission_reread": admission_reread,
            }
            if connector is not None:
                kwargs["connector"] = connector
            return cast(Any, adapter).bind_durable_commit_capability(admission, journal, **kwargs)
    except ProductionEffectBindingError:
        raise
    except Exception as error:
        raise ProductionEffectBindingError(
            "durable authority effect binding was rejected"
        ) from error
    raise ProductionEffectBindingError("unsupported concrete production adapter")


def bind_durable_rollback_capability(
    binding: LiveUpgradeBinding,
    admission: CommitAdmissionBundle,
    journal: object,
    rollback_effect: Callable[[object], object],
    *,
    session_revision: int,
) -> object:
    """Bind a backend-owned rollback effect without exposing public rollback."""
    _validate_common(
        binding,
        admission,
        session_revision,
        rollback_effect,
        expected_target="rollback",
        require_reread=False,
    )
    try:
        adapter = cast(Any, binding.adapter)
        if type(adapter) not in {GitAuthorityAdapter, SQLiteAuthorityAdapter}:
            raise ProductionEffectBindingError("unsupported concrete production adapter")
        return cast(Any, adapter).bind_durable_rollback_capability(
            admission,
            journal,
            session_revision=session_revision,
            rollback_effect=rollback_effect,
        )
    except ProductionEffectBindingError:
        raise
    except Exception as error:
        raise ProductionEffectBindingError(
            "durable rollback effect binding was rejected"
        ) from error
