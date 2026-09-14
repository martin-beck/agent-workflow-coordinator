# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Contract-aligned authority and staged-runtime selection helpers."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from tools import handoffctl


class AuthorityError(RuntimeError):
    """Raised when authority or staged-runtime identity is not proven."""


def inspect_authority() -> dict[str, object]:
    """Read and validate the selected backend using handoffctl's contracts."""
    try:
        selection = handoffctl.backend_selection()
        binding = handoffctl.project_binding()
        handoffctl._assert_storage_binding(binding)
    except Exception as error:
        raise AuthorityError("project/backend binding validation failed") from error
    result: dict[str, object] = {
        "backend": selection["backend"],
        "project_id": binding["project_id"],
        "state_repository": binding["state_repository"],
        "product_repository": binding["product_repository"],
        "legacy_backend": bool(selection.get("legacy", False)),
        "binding_valid": True,
        "backend_valid": True,
    }
    if selection["backend"] == "git":
        try:
            status = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=all"],  # noqa: S607
                cwd=handoffctl.ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise AuthorityError("Git authority inspection failed") from error
        if status.stdout:
            raise AuthorityError("Git authority is dirty")
        result["state_clean"] = True
    else:
        try:
            tasks = handoffctl.storage_backend().load_tasks()
        except Exception as error:
            raise AuthorityError("SQLite authority inspection failed") from error
        result["state_clean"] = True
        result["task_count"] = len(tasks)
    return result


def read_runtime_selector(path: Path) -> dict[str, Any]:
    """Read a separate staged-runtime selector; backend config is never changed."""
    if path.is_symlink() or not path.is_file():
        raise AuthorityError("runtime selector is absent or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AuthorityError("runtime selector is unreadable") from error
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "active_release",
        "previous_release",
    }:
        raise AuthorityError("runtime selector schema is invalid")
    if value["schema_version"] != 1 or not all(
        isinstance(value[key], str) and value[key] for key in ("active_release", "previous_release")
    ):
        raise AuthorityError("runtime selector identity is invalid")
    return value


def commit_runtime_selector(path: Path, active_release: str, previous_release: str) -> None:
    """Atomically publish a versioned runtime selector with no backend mutation."""
    if not active_release or not previous_release or path.is_symlink():
        raise AuthorityError("runtime selector identity is invalid")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "schema_version": 1,
                    "active_release": active_release,
                    "previous_release": previous_release,
                },
                stream,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise AuthorityError("runtime selector publication failed") from error
