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


class GitAuthorityError(RuntimeError):
    """Git authority evidence is unavailable or a mutation was requested."""


class GitAuthorityAdapter:
    """Read-only Git evidence adapter; execute remains permanently disabled."""

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
        return dict(context)

    def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, Any]:
        """Return identity-bound, read-only Git facts; never claim mutability evidence."""
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

    def verify_rollback_context(self, context: Mapping[str, object]) -> dict[str, Any]:
        """Reread clean Git identity but do not authorize rollback."""
        value = self.snapshot("rollback", context)
        value["rollback_context_verified"] = False
        return value

    def execute(self, _phase: str, _context: Mapping[str, object]) -> dict[str, Any]:
        raise GitAuthorityError("Git authority mutation adapter is not implemented")
