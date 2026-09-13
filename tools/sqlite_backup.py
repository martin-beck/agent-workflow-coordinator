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


def _install(source: Path, destination: Path, binding: dict[str, Any]) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".coordinator-backup-", suffix=".sqlite3", dir=destination.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary)
    try:
        source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        destination_connection = sqlite3.connect(temporary_path)
        try:
            source_connection.backup(destination_connection, pages=64, sleep=0.05)
            destination_connection.commit()
        finally:
            destination_connection.close()
            source_connection.close()
        _integrity(temporary_path, binding)
        temporary_path.chmod(0o600)
        temporary_path.replace(destination)
        directory = os.open(destination.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except sqlite3.Error as error:
        raise BackupError("online SQLite backup failed") from error
    finally:
        temporary_path.unlink(missing_ok=True)
        Path(str(temporary_path) + "-wal").unlink(missing_ok=True)
        Path(str(temporary_path) + "-shm").unlink(missing_ok=True)


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
    if set(manifest) != MANIFEST_FIELDS:
        raise BackupError("backup manifest has unknown or missing fields")
    required = {
        "schema_version": MANIFEST_VERSION,
        "kind": "sqlite-online-backup",
        "database_sha256": _digest(backup),
        "integrity_check": "ok",
        "foreign_key_check": "ok",
        "binding_verified": True,
        "wal_consistent": True,
    }
    if any(manifest.get(key) != value for key, value in required.items()):
        raise BackupError("backup manifest is missing or does not match the backup")
    _integrity(backup, binding)
    _install(backup, destination, binding)


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """Write a deterministic manifest without accepting non-JSON values."""
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
