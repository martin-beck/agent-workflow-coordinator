# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Bind concrete Git and SQLite rollback effects to the durable fence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from tools.authority_neutral_commit import CommitAdmissionBundle
from tools.git_authority_adapter import GitAuthorityAdapter, GitAuthorityError
from tools.production_effect_binding import _validate_common
from tools.sqlite_authority_adapter import SQLiteAuthorityAdapter, SQLiteAuthorityError
from tools.upgrade_binding import LiveUpgradeBinding


class ProductionRollbackEffectError(ValueError):
    """A concrete production rollback effect cannot be safely bound."""


def _manifest_identity(manifest: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(dict(manifest), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _git_manifest(backup: Path) -> dict[str, object]:
    try:
        value = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProductionRollbackEffectError("Git rollback manifest is unavailable") from error
    if not isinstance(value, dict):
        raise ProductionRollbackEffectError("Git rollback manifest is invalid")
    return value


def _receipt(admission: CommitAdmissionBundle, result: Mapping[str, object]) -> dict[str, object]:
    if result.get("mutates_authority") is not True or result.get("verified") is not True:
        raise ProductionRollbackEffectError("backend rollback evidence is incomplete")
    return {
        "backend": admission.backend,
        "target": admission.target,
        "operation_id": admission.operation_id,
        "state_revision": admission.state_revision,
        "barrier_id": admission.barrier_id,
        "artifact_identity": admission.artifact_identity,
        "manifest_identity": admission.manifest_identity,
        "selector_identity": admission.selector_identity,
        "runtime_identity": admission.runtime_identity,
        "fencing_token": admission.fencing_token,
        "mutates_authority": True,
    }


def bind_concrete_durable_rollback_capability(  # noqa: C901
    binding: LiveUpgradeBinding,
    admission: CommitAdmissionBundle,
    journal: object,
    backup: Path,
    *,
    session_revision: int,
    expected_branch: str | None = None,
    expected_head: str | None = None,
    sqlite_manifest: Mapping[str, object] | None = None,
    sqlite_binding: Mapping[str, object] | None = None,
) -> object:
    """Bind a backend-owned rollback operation to the durable journal.

    The backup and backend binding are supplied by the already-admitted phase
    boundary.  This factory does not expose a public dispatcher or fabricate
    admission evidence.
    """
    _validate_common(
        binding,
        admission,
        session_revision,
        dict,
        expected_target="rollback",
        expected_suffix="rollback",
        require_reread=False,
    )
    if not isinstance(backup, Path) or not backup.exists():
        raise ProductionRollbackEffectError("rollback backup is invalid")
    adapter = cast(Any, binding.adapter)

    if type(adapter) is GitAuthorityAdapter:
        if not backup.is_dir():
            raise ProductionRollbackEffectError("Git rollback backup directory is invalid")
        if not isinstance(expected_branch, str) or not isinstance(expected_head, str):
            raise ProductionRollbackEffectError("Git rollback identity is incomplete")
        adapter._check_repository_identity()
        manifest = _git_manifest(backup)
        verified = adapter.verify_backup_artifact(backup)
        if (
            verified.get("commit") != admission.artifact_identity
            or _manifest_identity(manifest) != admission.manifest_identity
        ):
            raise ProductionRollbackEffectError(
                "Git rollback backup identity does not match admission"
            )

        def effect(_argument: object) -> Mapping[str, object]:
            try:
                manifest = _git_manifest(backup)
                verified = adapter.verify_backup_artifact(backup)
                if (
                    verified.get("commit") != admission.artifact_identity
                    or _manifest_identity(manifest) != admission.manifest_identity
                ):
                    raise ProductionRollbackEffectError(
                        "Git rollback backup identity does not match admission"
                    )
                result = adapter.restore_authority_bound(
                    backup, expected_branch=expected_branch, expected_head=expected_head
                )
            except GitAuthorityError:
                raise
            except BaseException as error:
                raise ProductionRollbackEffectError("Git rollback failed") from error
            return _receipt(admission, result)

    elif type(adapter) is SQLiteAuthorityAdapter:
        if not backup.is_file():
            raise ProductionRollbackEffectError("SQLite rollback backup file is invalid")
        if not isinstance(sqlite_manifest, Mapping) or not isinstance(sqlite_binding, Mapping):
            raise ProductionRollbackEffectError("SQLite rollback binding is incomplete")
        adapter._check_identity()
        verified = adapter.verify_backup_artifact(
            backup, dict(sqlite_manifest), dict(sqlite_binding)
        )
        if (
            verified.get("database_sha256") != admission.artifact_identity
            or _manifest_identity(verified) != admission.manifest_identity
        ):
            raise ProductionRollbackEffectError(
                "SQLite rollback backup identity does not match admission"
            )

        def effect(_argument: object) -> Mapping[str, object]:
            try:
                adapter.restore_bound(
                    backup,
                    adapter._authority,
                    dict(sqlite_manifest),
                    dict(sqlite_binding),
                )
                with sqlite3.connect(adapter._authority) as connection:
                    integrity = connection.execute("PRAGMA integrity_check").fetchone()
                if integrity != ("ok",):
                    raise ProductionRollbackEffectError("SQLite rollback integrity check failed")
            except ProductionRollbackEffectError:
                raise
            except SQLiteAuthorityError:
                raise
            except BaseException as error:
                raise ProductionRollbackEffectError("SQLite rollback failed") from error
            return _receipt(admission, {"verified": True, "mutates_authority": True})

    else:
        raise ProductionRollbackEffectError("unsupported concrete production adapter")

    try:
        if type(adapter) is GitAuthorityAdapter:
            return adapter.bind_durable_rollback_capability(
                admission,
                journal,
                session_revision=session_revision,
                rollback_effect=effect,
            )
        return adapter.bind_durable_rollback_capability(
            admission,
            journal,
            session_revision=session_revision,
            rollback_effect=effect,
        )
    except ProductionRollbackEffectError:
        raise
    except Exception as error:
        raise ProductionRollbackEffectError("durable rollback binding was rejected") from error
