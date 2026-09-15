# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Read-only Git authority evidence for the future scoped adapter."""

# The executable and arguments are fixed by this adapter's Git observation contract.
# ruff: noqa: S603, S607

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tools.admission_lease import AdmissionLease, AdmissionRecheck
from tools.lock_domain_scope import LockDomainScope


class GitAuthorityError(RuntimeError):
    """Git authority evidence is unavailable or a mutation was requested."""


_SUPPORTED_PHASES = frozenset(
    {
        "discover",
        "preflight",
        "quiesce",
        "backup",
        "stage",
        "commit",
        "validate",
        "reopen",
        "rollback",
    }
)


class GitAuthorityAdapter:
    """Read-only Git evidence adapter; execute remains permanently disabled."""

    requires_bound_rollback = True
    bound_rollback_kind = "git"

    def __init__(self, repository: Path) -> None:
        resolved = repository.resolve()
        if not resolved.is_dir():
            raise GitAuthorityError("Git authority repository is unavailable")
        self._repository = resolved

    def _git(self, *arguments: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(self._repository), *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GitAuthorityError("Git authority observation failed") from error
        if result.returncode != 0:
            raise GitAuthorityError("Git authority observation was rejected")
        return result.stdout.strip()

    @staticmethod
    def _context(context: Mapping[str, object]) -> dict[str, object]:
        required = {
            "schema_version",
            "backend",
            "project_id",
            "operation_id",
            "state_revision",
            "authority_revision",
            "fencing_token",
            "fencing_owner",
            "durable_barrier_id",
            "artifact_root",
            "source",
            "destination",
            "manifest",
            "barrier_identity_digest",
            "target",
            "envelope_digest",
        }
        if set(context) != required or context.get("backend") != "git":
            raise GitAuthorityError("Git authority context is incomplete or mismatched")
        schema_version = context.get("schema_version")
        if type(schema_version) is not int or schema_version < 1:
            raise GitAuthorityError("Git authority context types are invalid")
        state_revision = context.get("state_revision")
        if type(state_revision) is not int or state_revision < 1:
            raise GitAuthorityError("Git authority context types are invalid")
        for field in required - {"schema_version", "state_revision"}:
            value = context.get(field)
            if type(value) is not str or not value:
                raise GitAuthorityError("Git authority context types are invalid")
        if context.get("target") not in {"new", "rollback"}:
            raise GitAuthorityError("Git authority context target is invalid")
        return dict(context)

    def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, Any]:
        """Return identity-bound, read-only Git facts; never claim mutability evidence."""
        if type(phase) is not str or phase not in _SUPPORTED_PHASES:
            raise GitAuthorityError("Git authority phase is invalid")
        value = self._context(context)
        status = self._git("status", "--porcelain=v1", "--untracked-files=all")
        head = self._git("rev-parse", "--verify", "HEAD")
        branch = self._git("symbolic-ref", "--short", "-q", "HEAD")
        if not head or not branch or status:
            raise GitAuthorityError("Git authority is not clean and branch-bound")
        value.update(
            {
                "phase": phase,
                "backend_identity_verified": True,
                "git_head": head,
                "git_branch": branch,
                "git_clean": True,
                "mutates_authority": False,
            }
        )
        return value

    def snapshot_bound(  # noqa: C901
        self,
        phase: str,
        context: Mapping[str, object],
        scope: LockDomainScope,
        *,
        lease: AdmissionLease,
        admission_recheck: AdmissionRecheck,
        expected_branch: str,
        expected_head: str,
    ) -> dict[str, Any]:
        """Read Git identity only inside a trusted session scope.

        The scope performs the durable lease/revision reread immediately before
        observation.  This remains an evidence-only seam; no Git mutation is
        reachable from it.
        """
        from tools.scoped_backend_adapter import ScopedBackendAdapter

        if type(phase) is not str or phase not in _SUPPORTED_PHASES:
            raise GitAuthorityError("Git authority phase is invalid")
        if type(expected_branch) is not str or not expected_branch:
            raise GitAuthorityError("expected Git branch identity is invalid")
        if type(expected_head) is not str or not expected_head:
            raise GitAuthorityError("expected Git head identity is invalid")
        if not isinstance(lease, AdmissionLease):
            raise GitAuthorityError("trusted admission lease is required")
        if not isinstance(admission_recheck, AdmissionRecheck):
            raise GitAuthorityError("trusted admission recheck is required")
        if admission_recheck.lease != lease:
            raise GitAuthorityError("trusted admission recheck does not match lease")
        try:
            validated_context = self._context(context)
        except GitAuthorityError:
            raise
        except Exception as error:
            raise GitAuthorityError("Git authority context is invalid") from error
        if not isinstance(scope, LockDomainScope):
            raise GitAuthorityError("concrete lock-domain scope is required")
        expected_identity = {
            "project_id": lease.project_id,
            "authority_revision": lease.authority_revision,
            "fencing_token": lease.fencing_token,
            "fencing_owner": lease.fencing_owner,
            "durable_barrier_id": lease.durable_barrier_id,
            "state_revision": lease.revision,
        }
        if any(validated_context.get(name) != value for name, value in expected_identity.items()):
            raise GitAuthorityError("trusted session identity changed")
        try:
            value = ScopedBackendAdapter(self, scope).snapshot(
                phase, validated_context, scope_context=expected_identity
            )
        except GitAuthorityError:
            raise
        except (TypeError, RuntimeError) as error:
            raise GitAuthorityError("trusted Git session reread was rejected") from error
        expected_result_keys = set(validated_context) | {
            "phase",
            "backend_identity_verified",
            "git_head",
            "git_branch",
            "git_clean",
            "mutates_authority",
        }
        if set(value) != expected_result_keys:
            raise GitAuthorityError("Git authority backend result schema changed")
        for field, expected in validated_context.items():
            if value.get(field) != expected or type(value.get(field)) is not type(expected):
                raise GitAuthorityError("Git authority backend context identity changed")
        if type(value.get("phase")) is not str or value.get("phase") != phase:
            raise GitAuthorityError("Git authority backend phase changed")
        if (
            type(value.get("backend_identity_verified")) is not bool
            or value.get("backend_identity_verified") is not True
        ):
            raise GitAuthorityError("Git authority backend identity is unverified")
        if (
            type(value.get("mutates_authority")) is not bool
            or value.get("mutates_authority") is not False
        ):
            raise GitAuthorityError("Git authority backend is not read-only")
        if type(value.get("git_head")) is not str or not value.get("git_head"):
            raise GitAuthorityError("Git authority backend head is unverified")
        if type(value.get("git_branch")) is not str or not value.get("git_branch"):
            raise GitAuthorityError("Git authority backend branch is unverified")
        if type(value.get("git_clean")) is not bool or value.get("git_clean") is not True:
            raise GitAuthorityError("Git authority backend cleanliness is unverified")
        if value.get("git_branch") != expected_branch or value.get("git_head") != expected_head:
            raise GitAuthorityError("Git authority identity changed")
        return value

    def verify_rollback_context(self, context: Mapping[str, object]) -> dict[str, Any]:
        """Reread clean Git identity but do not authorize rollback."""
        if context.get("target") != "rollback":
            raise GitAuthorityError("Git rollback context target is invalid")
        value = self.snapshot("rollback", context)
        value["rollback_context_verified"] = False
        return value

    def verify_rollback_context_bound(
        self,
        context: Mapping[str, object],
        scope: LockDomainScope,
        *,
        lease: AdmissionLease,
        admission_recheck: AdmissionRecheck,
        expected_branch: str,
        expected_head: str,
    ) -> dict[str, Any]:
        """Reread rollback evidence only inside a trusted scope."""
        if context.get("target") != "rollback":
            raise GitAuthorityError("Git rollback context target is invalid")
        value = self.snapshot_bound(
            "rollback",
            context,
            scope,
            lease=lease,
            admission_recheck=admission_recheck,
            expected_branch=expected_branch,
            expected_head=expected_head,
        )
        value["rollback_context_verified"] = False
        return value

    def execute(self, _phase: str, _context: Mapping[str, object]) -> dict[str, Any]:
        raise GitAuthorityError("Git authority mutation adapter is not implemented")
