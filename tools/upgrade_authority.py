# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Contract-aligned authority and staged-runtime selection helpers."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from tools.sqlite_storage import SCHEMA_VERSION as SQLITE_SCHEMA_VERSION


class AuthorityError(RuntimeError):
    """Raised when authority or staged-runtime identity is not proven."""


_AUTHORITY_TABLES = {
    "metadata": ("key", "value"),
    "tasks": (
        "id",
        "filename",
        "meta_json",
        "body",
        "revision",
        "status",
        "owner",
        "claim_expires",
        "branch",
        "worktree_key",
        "updated_at",
    ),
    "dependencies": ("task_id", "dependency_id"),
    "events": ("sequence", "task_id", "revision", "kind", "recorded_at", "note"),
    "command_results": (
        "sequence",
        "task_id",
        "owner",
        "argv_sha256",
        "returncode",
        "classification",
        "recorded_at",
    ),
    "migrations": (
        "sequence",
        "source_backend",
        "source_checkpoint",
        "imported_at",
        "finalized",
    ),
    "checkpoints": ("name", "revision", "recorded_at"),
    "sqlite_sequence": ("name", "seq"),
}
_AUTHORITY_ORDER = {
    "metadata": "key",
    "tasks": "id",
    "dependencies": "task_id,dependency_id",
    "events": "sequence",
    "command_results": "sequence",
    "migrations": "sequence",
    "checkpoints": "name",
    "sqlite_sequence": "name",
}
_SQLITE_SIDECARS = ("-wal", "-shm")


@dataclass(frozen=True, slots=True)
class SQLiteReleaseAuthoritySnapshot:
    """Canonical facts reread from one restored SQLite authority and runtime selector."""

    project_id: str
    authority_revision: str
    active_release: str
    previous_release: str
    integrity_check: str
    foreign_key_violations: int


def _file_identity(status: os.stat_result) -> tuple[int, int]:
    return status.st_dev, status.st_ino


def _open_parent(path: Path) -> tuple[int, tuple[int, int]]:
    if not path.is_absolute() or ".." in path.parts or path.name in {"", ".", ".."}:
        raise AuthorityError("authority path is not canonical and absolute")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parent.parts[1:]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor, _file_identity(os.fstat(descriptor))
    except OSError as error:
        os.close(descriptor)
        raise AuthorityError("authority parent descriptor is unsafe") from error


def _open_regular(
    path: Path, *, expected_parent: tuple[int, int] | None = None
) -> tuple[int, int, tuple[int, int], tuple[int, int]]:
    parent, parent_identity = _open_parent(path)
    descriptor = -1
    try:
        if expected_parent is not None and parent_identity != expected_parent:
            raise AuthorityError("authority parent identity changed")
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise AuthorityError("authority file is not a private regular file")
        return parent, descriptor, parent_identity, _file_identity(status)
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)
        raise AuthorityError("authority file descriptor is unsafe") from error
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)
        raise


def _recheck_regular(
    path: Path, parent_identity: tuple[int, int], identity: tuple[int, int]
) -> None:
    parent, descriptor, _, current = _open_regular(path, expected_parent=parent_identity)
    try:
        if current != identity:
            raise AuthorityError("authority file identity changed")
    finally:
        os.close(descriptor)
        os.close(parent)


def _sidecar_identities(parent: int, name: str) -> dict[str, tuple[int, int] | None]:
    result: dict[str, tuple[int, int] | None] = {}
    for suffix in _SQLITE_SIDECARS:
        descriptor = -1
        try:
            descriptor = os.open(name + suffix, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                raise AuthorityError("SQLite authority sidecar is not a private regular file")
            result[suffix] = _file_identity(status)
        except FileNotFoundError:
            result[suffix] = None
        except OSError as error:
            raise AuthorityError("SQLite authority sidecar descriptor is unsafe") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    return result


def _read_bound_json(path: Path, *, label: str) -> dict[str, Any]:
    parent, descriptor, parent_identity, identity = _open_regular(path)
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 8192)
            if not chunk:
                break
            total += len(chunk)
            if total > 64 * 1024:
                raise AuthorityError(f"{label} is too large")
            chunks.append(chunk)
        try:
            value = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AuthorityError(f"{label} is unreadable") from error
        _recheck_regular(path, parent_identity, identity)
    finally:
        os.close(descriptor)
        os.close(parent)
    if not isinstance(value, dict):
        raise AuthorityError(f"{label} schema is invalid")
    return cast(dict[str, Any], value)


def _read_backend_selector(path: Path, project_id: str) -> dict[str, object]:
    value = _read_bound_json(path, label="backend selector")
    if set(value) != {"schema_version", "project_id", "backend"} or value != {
        "schema_version": 1,
        "project_id": project_id,
        "backend": "sqlite",
    }:
        raise AuthorityError("backend selector identity is invalid")
    return cast(dict[str, object], value)


def _read_project_binding(path: Path, project_id: str) -> dict[str, object]:
    value = _read_bound_json(path, label="project binding")
    if (
        set(value) != {"schema_version", "project_id", "state_repository", "product_repository"}
        or value.get("schema_version") != 1
        or value.get("project_id") != project_id
        or not all(
            isinstance(value.get(key), str) and value[key]
            for key in ("state_repository", "product_repository")
        )
    ):
        raise AuthorityError("project binding identity is invalid")
    return cast(dict[str, object], value)


def _read_runtime_selector_bound(path: Path) -> dict[str, Any]:
    value = _read_bound_json(path, label="runtime selector")
    if set(value) != {"schema_version", "active_release", "previous_release"}:
        raise AuthorityError("runtime selector schema is invalid")
    if value["schema_version"] != 1 or not all(
        isinstance(value[key], str) and value[key] for key in ("active_release", "previous_release")
    ):
        raise AuthorityError("runtime selector identity is invalid")
    return value


def _authority_rows(connection: sqlite3.Connection) -> dict[str, object]:
    schema = [
        list(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    ]
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if tables != set(_AUTHORITY_TABLES) - {"sqlite_sequence"}:
        raise AuthorityError("SQLite authority schema is invalid")
    result: dict[str, object] = {"schema": schema}
    for table, columns in _AUTHORITY_TABLES.items():
        selected = ",".join(columns)
        order = _AUTHORITY_ORDER[table]
        rows = [
            list(row)
            for row in connection.execute(
                f"SELECT {selected} FROM {table} ORDER BY {order}"  # noqa: S608
            )
        ]
        if any(type(item) not in {str, int, type(None)} for row in rows for item in row):
            raise AuthorityError("SQLite authority contains non-canonical values")
        result[table] = rows
    return result


def inspect_sqlite_release_authority(  # noqa: C901
    authority_path: Path,
    project_binding_path: Path,
    backend_selector_path: Path,
    runtime_selector_path: Path,
    project_id: str,
    active_release: str,
    previous_release: str,
) -> SQLiteReleaseAuthoritySnapshot:
    """Reread and canonically hash an exact SQLite authority/runtime release pair."""
    if not all(
        isinstance(value, str) and value for value in (project_id, active_release, previous_release)
    ):
        raise AuthorityError("release-specific authority identity is invalid")
    project_binding = _read_project_binding(project_binding_path, project_id)
    backend_selector = _read_backend_selector(backend_selector_path, project_id)
    runtime_selector = _read_runtime_selector_bound(runtime_selector_path)
    if runtime_selector["active_release"] != active_release or (
        runtime_selector["previous_release"] != previous_release
    ):
        raise AuthorityError("runtime selector release identity changed")

    parent, descriptor, parent_identity, identity = _open_regular(authority_path)
    connection: sqlite3.Connection | None = None
    try:
        before_sidecars = _sidecar_identities(parent, authority_path.name)
        connection = sqlite3.connect(
            f"file:/proc/self/fd/{descriptor}?mode=ro", isolation_level=None, uri=True
        )
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN")
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_violations = len(list(connection.execute("PRAGMA foreign_key_check")))
        rows = _authority_rows(connection)
        bound_sidecars = _sidecar_identities(parent, authority_path.name)
        if any(
            before_sidecars[suffix] not in {None, bound_sidecars[suffix]}
            for suffix in _SQLITE_SIDECARS
        ):
            raise AuthorityError("SQLite authority sidecar identity changed")
        metadata_rows = cast(list[list[object]], rows["metadata"])
        metadata = {str(row[0]): row[1] for row in metadata_rows}
        expected_metadata = {
            "schema_version": str(SQLITE_SCHEMA_VERSION),
            "backend": "sqlite",
            "project_id": project_id,
            "state": "active",
        }
        if (
            set(metadata)
            != {
                "schema_version",
                "backend",
                "project_id",
                "state_repository",
                "product_repository",
                "state",
            }
            or any(metadata.get(key) != value for key, value in expected_metadata.items())
            or metadata.get("state_repository") != project_binding["state_repository"]
            or metadata.get("product_repository") != project_binding["product_repository"]
        ):
            raise AuthorityError("SQLite authority binding is invalid")
        tasks = cast(list[list[object]], rows["tasks"])
        for task in tasks:
            try:
                task_meta = json.loads(cast(str, task[2]))
            except (TypeError, json.JSONDecodeError) as error:
                raise AuthorityError("SQLite authority task JSON is invalid") from error
            expected = {
                "id": task[0],
                "task_revision": task[4],
                "status": task[5],
                "owner": task[6],
                "claim_expires": task[7],
                "branch": task[8],
                "worktree_key": task[9],
                "updated_at": task[10],
            }
            if not isinstance(task_meta, dict) or any(
                task_meta.get(key, "") != value for key, value in expected.items()
            ):
                raise AuthorityError("SQLite authority task projections disagree")
        payload = {
            "schema_version": 1,
            "kind": "agent-workflow-coordinator-sqlite-authority-revision",
            "project_binding": project_binding,
            "backend_selector": backend_selector,
            "runtime_selector": runtime_selector,
            "authority": rows,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        revision = hashlib.sha256(encoded).hexdigest()
        if _sidecar_identities(parent, authority_path.name) != bound_sidecars:
            raise AuthorityError("SQLite authority sidecar identity changed")
        _recheck_regular(authority_path, parent_identity, identity)
        if (
            _read_project_binding(project_binding_path, project_id) != project_binding
            or _read_backend_selector(backend_selector_path, project_id) != backend_selector
            or _read_runtime_selector_bound(runtime_selector_path) != runtime_selector
        ):
            raise AuthorityError("authority selector identity changed")
    except sqlite3.Error as error:
        raise AuthorityError("SQLite authority reread failed") from error
    finally:
        if connection is not None:
            connection.close()
        os.close(descriptor)
        os.close(parent)
    return SQLiteReleaseAuthoritySnapshot(
        project_id=project_id,
        authority_revision=revision,
        active_release=active_release,
        previous_release=previous_release,
        integrity_check=integrity,
        foreign_key_violations=foreign_key_violations,
    )


def _handoffctl() -> Any:
    """Load handoffctl package-safely, without changing import paths."""
    try:
        return importlib.import_module("tools.handoffctl")
    except ModuleNotFoundError:
        try:
            return importlib.import_module("handoffctl")
        except ModuleNotFoundError as fallback_error:
            raise AuthorityError(
                "handoffctl authority inspection is unavailable"
            ) from fallback_error


def inspect_authority() -> dict[str, object]:
    """Read and validate the selected backend using handoffctl's contracts."""
    handoffctl = cast(Any, _handoffctl())
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
    return _read_runtime_selector_bound(path)


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
