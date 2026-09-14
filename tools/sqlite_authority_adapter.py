# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Read-only SQLite authority evidence for the future scoped adapter."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class SQLiteAuthorityError(RuntimeError):
    """SQLite authority evidence is unavailable or mutation was requested."""


class SQLiteAuthorityAdapter:
    """Read-only integrity evidence adapter; execute remains disabled."""

    def __init__(self, authority: Path) -> None:
        resolved = authority.resolve()
        try:
            parent = resolved.parent.stat()
            descriptor = resolved.stat()
        except OSError as error:
            raise SQLiteAuthorityError("SQLite authority is unavailable") from error
        if (
            not stat.S_ISREG(descriptor.st_mode)
            or descriptor.st_uid != os.geteuid()
            or descriptor.st_nlink != 1
            or stat.S_IMODE(descriptor.st_mode) != 0o600
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise SQLiteAuthorityError("SQLite authority descriptor is unsafe")
        self._authority = resolved
        self._parent_identity = (parent.st_dev, parent.st_ino)
        self._descriptor_identity = (
            descriptor.st_dev,
            descriptor.st_ino,
            stat.S_IMODE(descriptor.st_mode),
            descriptor.st_uid,
            descriptor.st_nlink,
        )
        self._sidecar_identities = {
            suffix: self._optional_identity(resolved.with_name(resolved.name + suffix))
            for suffix in ("-wal", "-shm")
        }

    @staticmethod
    def _optional_identity(path: Path) -> tuple[int, int, int, int, int] | None:
        try:
            value = path.stat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise SQLiteAuthorityError("SQLite authority sidecar is unavailable") from error
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_uid != os.geteuid()
            or value.st_nlink != 1
            or stat.S_IMODE(value.st_mode) != 0o600
        ):
            raise SQLiteAuthorityError("SQLite authority sidecar is unsafe")
        return (
            value.st_dev,
            value.st_ino,
            stat.S_IMODE(value.st_mode),
            value.st_uid,
            value.st_nlink,
        )

    def _check_identity(self) -> None:
        try:
            parent = self._authority.parent.stat()
            descriptor = self._authority.stat()
        except OSError as error:
            raise SQLiteAuthorityError("SQLite authority identity changed") from error
        current_parent = (parent.st_dev, parent.st_ino)
        current_descriptor = (
            descriptor.st_dev,
            descriptor.st_ino,
            stat.S_IMODE(descriptor.st_mode),
            descriptor.st_uid,
            descriptor.st_nlink,
        )
        if (
            current_parent != self._parent_identity
            or current_descriptor != self._descriptor_identity
            or self._sidecar_identities
            != {
                suffix: self._optional_identity(
                    self._authority.with_name(self._authority.name + suffix)
                )
                for suffix in ("-wal", "-shm")
            }
        ):
            raise SQLiteAuthorityError("SQLite authority identity changed")

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
        if set(context) != required or context.get("backend") != "sqlite":
            raise SQLiteAuthorityError("SQLite authority context is incomplete or mismatched")
        return dict(context)

    def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, Any]:
        """Return read-only integrity evidence without claiming mutation safety."""
        value = self._context(context)
        self._check_identity()
        try:
            connection = sqlite3.connect(
                f"file:{self._authority}?mode=ro", uri=True, isolation_level=None
            )
            try:
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                foreign = list(connection.execute("PRAGMA foreign_key_check"))
            finally:
                connection.close()
            self._check_identity()
        except (OSError, sqlite3.Error, TypeError, IndexError) as error:
            raise SQLiteAuthorityError("SQLite authority observation failed") from error
        if integrity != "ok" or foreign:
            raise SQLiteAuthorityError("SQLite authority integrity is not clean")
        value.update(
            {
                "phase": phase,
                "backend_identity_verified": True,
                "sqlite_integrity_verified": True,
                "sqlite_foreign_keys_verified": True,
                "mutates_authority": False,
            }
        )
        return value

    def verify_rollback_context(self, context: Mapping[str, object]) -> dict[str, Any]:
        value = self.snapshot("rollback", context)
        value["rollback_context_verified"] = False
        return value

    def execute(self, _phase: str, _context: Mapping[str, object]) -> dict[str, Any]:
        raise SQLiteAuthorityError("SQLite authority mutation adapter is not implemented")
