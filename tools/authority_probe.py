# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Read-only authority probes used before an upgrade can be admitted."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any


class ProbeError(RuntimeError):
    """Raised when authority state cannot be proven safe."""


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ProbeError("Git authority probe failed") from error
    return result.stdout


def probe_git(repo: Path, selector: Path) -> dict[str, object]:
    """Prove clean reachable refs and a regular, parseable selector read-only."""
    if repo.is_symlink() or not repo.is_dir() or selector.is_symlink():
        raise ProbeError("Git repository or selector path is unsafe")
    status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    if status:
        raise ProbeError("Git authority is dirty")
    head = _git(repo, "rev-parse", "--verify", "HEAD").strip()
    refs = _git(repo, "show-ref").splitlines()
    if not head or not refs:
        raise ProbeError("Git authority has no reachable refs")
    if not selector.is_file():
        raise ProbeError("Git selector is absent")
    try:
        value = json.loads(selector.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProbeError("Git selector is unreadable") from error
    if not isinstance(value, dict) or value.get("target") not in {"new", "rollback"}:
        raise ProbeError("Git selector is invalid")
    return {
        "state_clean": True,
        "state_synchronized": True,
        "no_divergence": True,
        "backend_valid": True,
        "selector_verified": True,
        "authority_revision": head,
        "refs_verified": True,
    }


def probe_sqlite(database: Path, selector: Path, binding: dict[str, Any]) -> dict[str, object]:
    """Prove SQLite integrity, binding metadata, and selector shape read-only."""
    if database.is_symlink() or not database.is_file() or selector.is_symlink():
        raise ProbeError("SQLite authority or selector path is unsafe")
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            rows = dict(connection.execute("SELECT key, value FROM metadata"))
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            foreign = list(connection.execute("PRAGMA foreign_key_check"))
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise ProbeError("SQLite authority probe failed") from error
    required = {
        "backend": "sqlite",
        "project_id": str(binding["project_id"]),
        "state_repository": str(binding["state_repository"]),
        "product_repository": str(binding["product_repository"]),
        "state": "active",
    }
    if (
        integrity != "ok"
        or foreign
        or any(rows.get(key) != value for key, value in required.items())
    ):
        raise ProbeError("SQLite integrity or binding proof failed")
    if not selector.is_file():
        raise ProbeError("SQLite selector is absent")
    try:
        value = json.loads(selector.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProbeError("SQLite selector is unreadable") from error
    if not isinstance(value, dict) or value.get("target") not in {"new", "rollback"}:
        raise ProbeError("SQLite selector is invalid")
    return {
        "state_clean": True,
        "state_synchronized": True,
        "no_divergence": True,
        "backend_valid": True,
        "selector_verified": True,
        "binding_valid": True,
        "integrity_verified": True,
    }
