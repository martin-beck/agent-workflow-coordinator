# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Fail-closed online backup and restore helpers for SQLite authority."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

MANIFEST_VERSION = 1
MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "database_sha256",
        "integrity_check",
        "foreign_key_check",
        "binding_verified",
        "wal_consistent",
    }
)


class BackupError(RuntimeError):
    """Raised when a SQLite backup or restore cannot be proven safe."""


def _regular(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise BackupError(f"{label} must be a regular file")


def _safe_parent(path: Path, label: str) -> None:
    """Reject symlinked lexical ancestors before allocating output files."""
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:-1]:
        current /= component
        if current.is_symlink():
            raise BackupError(f"{label} parent must not contain symlinks")


def _parent_identity(path: Path) -> tuple[int, int]:
    try:
        status = path.parent.stat()
    except OSError as error:
        raise BackupError("SQLite destination parent is unavailable") from error
    return status.st_dev, status.st_ino


def _assert_parent_identity(path: Path, expected: tuple[int, int]) -> None:
    if _parent_identity(path) != expected:
        raise BackupError("SQLite destination parent identity changed")


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _binding(connection: sqlite3.Connection, expected: dict[str, Any]) -> None:
    try:
        rows = dict(connection.execute("SELECT key, value FROM metadata"))
    except sqlite3.Error as error:
        raise BackupError("backup database has no valid metadata table") from error
    required = {
        "schema_version": "1",
        "backend": "sqlite",
        "project_id": str(expected["project_id"]),
        "state_repository": str(expected["state_repository"]),
        "product_repository": str(expected["product_repository"]),
        "state": "active",
    }
    if any(rows.get(key) != value for key, value in required.items()):
        raise BackupError("backup database binding or active-state check failed")


def _integrity(path: Path, binding: dict[str, Any]) -> None:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            result = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            foreign = list(connection.execute("PRAGMA foreign_key_check"))
            _binding(connection, binding)
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise BackupError("backup database cannot be opened for integrity verification") from error
    if result != "ok" or foreign:
        raise BackupError("backup database integrity verification failed")


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        raise BackupError(f"failed to clean up temporary file {path.name}") from error


def _copy_online(source: Path, temporary: Path, binding: dict[str, Any]) -> None:
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        destination_connection = sqlite3.connect(temporary)
        source_connection.backup(destination_connection, pages=64, sleep=0.05)
        destination_connection.commit()
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
    _integrity(temporary, binding)


def _backup_existing(destination: Path) -> Path | None:
    if not destination.exists():
        return None
    _regular(destination, "existing destination")
    try:
        destination_status = destination.stat()
    except OSError as error:
        raise BackupError("existing destination disappeared") from error
    destination_identity = (destination_status.st_dev, destination_status.st_ino)
    descriptor, previous = tempfile.mkstemp(
        prefix=".coordinator-previous-", suffix=".sqlite3", dir=destination.parent
    )
    os.close(descriptor)
    previous_path = Path(previous)
    try:
        previous_path.unlink()
        os.link(destination, previous_path)
        current_status = destination.stat()
        current_identity = (current_status.st_dev, current_status.st_ino)
        if current_identity != destination_identity:
            raise BackupError("existing destination changed during preservation")
    except (OSError, BackupError) as error:
        _unlink(previous_path)
        if isinstance(error, BackupError):
            raise
        raise BackupError("failed to preserve existing SQLite destination") from error
    return previous_path


def _restore_existing(destination: Path, previous: Path | None) -> None:
    try:
        if previous is None:
            destination.unlink(missing_ok=True)
        else:
            previous.replace(destination)
            _fsync_directory(destination.parent)
    except (OSError, BackupError) as error:
        raise BackupError(
            "SQLite installation failed and authority restore was ambiguous"
        ) from error


def _cleanup(paths: tuple[Path | None, ...]) -> None:
    cleanup_error: BackupError | None = None
    for path in paths:
        if path is not None:
            try:
                _unlink(path)
            except BackupError as error:
                cleanup_error = error
    if cleanup_error is not None:
        raise cleanup_error


def _install(source: Path, destination: Path, binding: dict[str, Any]) -> None:
    _safe_parent(destination, "SQLite destination")
    try:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_identity = _parent_identity(destination)
        _assert_parent_identity(destination, parent_identity)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".coordinator-backup-", suffix=".sqlite3", dir=destination.parent
        )
        os.close(descriptor)
    except OSError as error:
        raise BackupError("failed to allocate temporary SQLite destination") from error
    temporary_path = Path(temporary)
    previous_path: Path | None = None
    replaced = False
    try:
        _copy_online(source, temporary_path, binding)
        temporary_path.chmod(0o600)
        # Do not allocate a preservation link in a parent that was replaced
        # while the online copy was running.
        _assert_parent_identity(destination, parent_identity)
        previous_path = _backup_existing(destination)
        _assert_parent_identity(destination, parent_identity)
        temporary_path.replace(destination)
        replaced = True
        _fsync_directory(destination.parent)
    except (sqlite3.Error, OSError, BackupError) as error:
        if replaced:
            _restore_existing(destination, previous_path)
            previous_path = None
        if isinstance(error, BackupError):
            raise
        raise BackupError("online SQLite backup or installation failed") from error
    finally:
        _cleanup(
            (
                temporary_path,
                Path(str(temporary_path) + "-wal"),
                Path(str(temporary_path) + "-shm"),
                previous_path,
            )
        )


def backup_database(source: Path, destination: Path, binding: dict[str, Any]) -> dict[str, Any]:
    """Create a verified consistent backup without copying a live main file."""
    _regular(source, "source database")
    if destination.exists() or destination.is_symlink():
        raise BackupError("refusing to overwrite an existing backup")
    _integrity(source, binding)
    _install(source, destination, binding)
    return {
        "schema_version": MANIFEST_VERSION,
        "kind": "sqlite-online-backup",
        "database_sha256": _digest(destination),
        "integrity_check": "ok",
        "foreign_key_check": "ok",
        "binding_verified": True,
        "wal_consistent": True,
    }


def restore_database(
    backup: Path,
    destination: Path,
    manifest: dict[str, Any],
    binding: dict[str, Any],
    *,
    quiesced: bool,
) -> None:
    """Install a verified backup atomically; refuse restore while writers may run."""
    if not quiesced:
        raise BackupError("restore requires a proven quiesced authority")
    if Path(str(destination) + "-wal").exists() or Path(str(destination) + "-shm").exists():
        raise BackupError("restore requires a checkpointed destination without live WAL sidecars")
    _regular(backup, "backup database")
    _validate_manifest(manifest)
    if manifest.get("database_sha256") != _digest(backup):
        raise BackupError("backup manifest is missing or does not match the backup")
    _integrity(backup, binding)
    _install(backup, destination, binding)


def verify_backup(
    backup: Path, manifest: dict[str, Any], binding: dict[str, Any]
) -> dict[str, Any]:
    """Verify a SQLite backup and manifest without installing or mutating it."""
    _regular(backup, "backup database")
    _validate_manifest(manifest)
    if manifest.get("database_sha256") != _digest(backup):
        raise BackupError("backup manifest is missing or does not match the backup")
    _integrity(backup, binding)
    return dict(manifest)


def _validate_manifest(manifest: dict[str, Any]) -> None:
    if set(manifest) != MANIFEST_FIELDS:
        raise BackupError("backup manifest has unknown or missing fields")
    required = {
        "schema_version": MANIFEST_VERSION,
        "kind": "sqlite-online-backup",
        "integrity_check": "ok",
        "foreign_key_check": "ok",
        "binding_verified": True,
        "wal_consistent": True,
    }
    if any(manifest.get(key) != value for key, value in required.items()):
        raise BackupError("backup manifest has invalid verification claims")


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """Write a deterministic manifest without accepting non-JSON values."""
    _validate_manifest(manifest)
    _safe_parent(path, "SQLite manifest")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_identity = _parent_identity(path)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".coordinator-manifest-", suffix=".json", dir=path.parent
        )
        temporary_path = Path(temporary)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        _assert_parent_identity(path, parent_identity)
        temporary_path.replace(path)
        _fsync_directory(path.parent)
    except (OSError, TypeError, ValueError) as error:
        raise BackupError("atomic manifest publication failed") from error
    finally:
        if "temporary_path" in locals():
            _unlink(temporary_path)
