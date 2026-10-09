# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Private durable intents for the opt-in Git receipt protocol.

This module does not execute transitions or issue completion acknowledgements.
Only a bound service may turn an intent into a signed authority commit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from pathlib import Path
from typing import Any

if __package__:
    from .sqlite_storage import require_local_filesystem
else:  # pragma: no cover - direct vendored import
    from sqlite_storage import require_local_filesystem  # type: ignore[import-not-found,no-redef]

_TOKEN = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_PHASE_QUEUED = "queued-local"


class ReceiptConflictError(RuntimeError):
    """One idempotency key was submitted with different canonical content."""


class ReceiptStore:
    """A per-Git-common-directory SQLite WAL intent queue."""

    def __init__(self, path: Path, project_id: str) -> None:
        uuid.UUID(project_id)
        self.project_id = project_id
        self.path = path
        if sqlite3.sqlite_version_info < (3, 37, 0):
            raise RuntimeError("SQLITE_VERSION_UNSUPPORTED: SQLite 3.37 or newer is required")
        require_local_filesystem(path)
        self._parent_fd, self._database_fd = self._open_private_path()
        try:
            self.connection = sqlite3.connect(path, timeout=10, isolation_level=None)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA busy_timeout=10000")
            mode = str(self.connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            if mode != "wal":
                raise RuntimeError("SQLITE_WAL_UNAVAILABLE: journal mode is not WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self._assert_path()
            self._initialize()
            self._closed = False
        except BaseException:
            if hasattr(self, "connection"):
                self.connection.close()
            os.close(self._database_fd)
            os.close(self._parent_fd)
            raise

    def _open_private_path(self) -> tuple[int, int]:
        parent = self.path.parent
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise RuntimeError("receipt directory is not private")
        if self.path.is_symlink():
            raise RuntimeError("receipt database must not be a symlink")
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                database_fd = os.open(
                    self.path.name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                database_fd = os.open(self.path.name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=parent_fd)
            info = os.fstat(database_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                os.close(database_fd)
                raise RuntimeError("receipt database is not a private regular owned file")
            return parent_fd, database_fd
        except BaseException:
            os.close(parent_fd)
            raise

    def _assert_path(self) -> None:
        parent = os.fstat(self._parent_fd)
        current_parent = self.path.parent.lstat()
        database = os.fstat(self._database_fd)
        current_database = os.stat(self.path.name, dir_fd=self._parent_fd, follow_symlinks=False)
        if (
            (parent.st_dev, parent.st_ino) != (current_parent.st_dev, current_parent.st_ino)
            or current_parent.st_uid != os.geteuid()
            or stat.S_IMODE(current_parent.st_mode) & 0o077
            or (database.st_dev, database.st_ino)
            != (current_database.st_dev, current_database.st_ino)
            or not stat.S_ISREG(current_database.st_mode)
            or current_database.st_nlink != 1
            or stat.S_IMODE(current_database.st_mode) != 0o600
        ):
            raise RuntimeError("receipt database path identity changed")
        for suffix in ("-wal", "-shm"):
            try:
                sidecar = os.stat(
                    self.path.name + suffix, dir_fd=self._parent_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(sidecar.st_mode)
                or sidecar.st_uid != os.geteuid()
                or sidecar.st_nlink != 1
                or stat.S_IMODE(sidecar.st_mode) != 0o600
            ):
                raise RuntimeError("receipt database sidecar is unsafe")

    def _initialize(self) -> None:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("unsupported receipt database schema")
            self.connection.execute(
                """CREATE TABLE IF NOT EXISTS receipt_binding (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    project_id TEXT NOT NULL
                )"""
            )
            binding = self.connection.execute(
                "SELECT project_id FROM receipt_binding WHERE singleton=1"
            ).fetchone()
            if binding is None:
                self.connection.execute(
                    "INSERT INTO receipt_binding(singleton, project_id) VALUES (1, ?)",
                    (self.project_id,),
                )
            elif binding["project_id"] != self.project_id:
                raise RuntimeError("receipt database is bound to a different project")
            self.connection.execute(
                """CREATE TABLE IF NOT EXISTS intents (
                    receipt_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    expected_revision INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    input_digest TEXT NOT NULL,
                    phase TEXT NOT NULL CHECK (phase IN ('queued-local', 'running')),
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    UNIQUE(project_id, idempotency_key)
                )"""
            )
            self.connection.execute("PRAGMA user_version=1")
            self._assert_path()
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        self._assert_path()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.connection.close()
        os.close(self._database_fd)
        os.close(self._parent_fd)

    def enqueue_heartbeat(
        self,
        *,
        key: str,
        task: str,
        owner: str,
        expected_revision: int,
        lease_minutes: int,
    ) -> dict[str, Any]:
        """Fsync a typed intent; no task transition has happened at return."""
        if not _TOKEN.fullmatch(key) or not _TOKEN.fullmatch(task):
            raise ValueError("invalid receipt key or task")
        if not _TOKEN.fullmatch(owner):
            raise ValueError("invalid owner")
        if expected_revision < 1 or not 1 <= lease_minutes <= 1440:
            raise ValueError("invalid revision or lease")
        payload = {
            "expected_revision": expected_revision,
            "lease_minutes": lease_minutes,
            "operation": "heartbeat",
            "owner": owner,
            "project_id": self.project_id,
            "task": task,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self._assert_path()
        self.connection.execute("BEGIN IMMEDIATE")
        committed = False
        try:
            existing = self.connection.execute(
                "SELECT * FROM intents WHERE project_id=? AND idempotency_key=?",
                (self.project_id, key),
            ).fetchone()
            if existing is None:
                receipt_id = uuid.uuid4().hex
                self.connection.execute(
                    """INSERT INTO intents
                    (receipt_id, project_id, idempotency_key, operation, task_id,
                     expected_revision, payload_json, input_digest, phase)
                    VALUES (?, ?, ?, 'heartbeat', ?, ?, ?, ?, ?)""",
                    (
                        receipt_id,
                        self.project_id,
                        key,
                        task,
                        expected_revision,
                        canonical,
                        digest,
                        _PHASE_QUEUED,
                    ),
                )
                existing = self.connection.execute(
                    "SELECT * FROM intents WHERE receipt_id=?", (receipt_id,)
                ).fetchone()
            if existing is None:
                raise RuntimeError("receipt insert was not visible")
            if existing["input_digest"] != digest or existing["payload_json"] != canonical:
                raise ReceiptConflictError("idempotency key already binds different input")
            self._assert_path()
            self.connection.execute("COMMIT")
            committed = True
            self._assert_path()
            return dict(existing)
        except BaseException:
            if not committed:
                self.connection.execute("ROLLBACK")
            raise

    def read(self, receipt_id: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[0-9a-f]{32}", receipt_id):
            raise ValueError("invalid receipt id")
        self._assert_path()
        row = self.connection.execute(
            "SELECT * FROM intents WHERE project_id=? AND receipt_id=?",
            (self.project_id, receipt_id),
        ).fetchone()
        self._assert_path()
        return None if row is None else dict(row)
