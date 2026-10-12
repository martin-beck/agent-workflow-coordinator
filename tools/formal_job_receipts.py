# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Candidate-fenced receipts for formal jobs that run outside coordinator authority.

This is deliberately a small private SQLite journal.  A caller holds a database
transaction only while it changes a receipt state; it never holds one while TLC
or another formal executor is running.  The receipt can consequently be used by
the coordinator as evidence after execution without turning a long model check
into an authority or coordinator-lock holder.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "superseded"})
RUNNABLE_STATES = frozenset({"started", "running"})
RESOURCE_CLASSES = frozenset(
    {"ordinary", "full-exhaustive", "storage-exhausted", "timeout", "admission-failed"}
)


class FormalJobError(RuntimeError):
    """Raised when a formal-job lifecycle transition is fenced or invalid."""


def _digest(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise FormalJobError("tier digest must be a lowercase SHA-256 digest")
    return value


def _candidate(value: str) -> str:
    if len(value) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise FormalJobError("candidate must be a lowercase Git object id")
    return value


def _resource_class(value: str) -> str:
    if value not in RESOURCE_CLASSES:
        raise FormalJobError("unknown formal-job resource classification")
    return value


class FormalJobStore:
    """Durable, revision-fenced formal-job receipts in a private SQLite file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._guard = threading.Lock()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS formal_jobs (
                    job_id TEXT PRIMARY KEY, scope TEXT NOT NULL,
                    candidate TEXT NOT NULL, tier_digest TEXT NOT NULL,
                    resource_class TEXT NOT NULL, state TEXT NOT NULL,
                    revision INTEGER NOT NULL, execution_token TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL,
                    receipt_json TEXT,
                    UNIQUE(scope, candidate, tier_digest, resource_class)
                ) STRICT"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS formal_jobs_active_scope "
                "ON formal_jobs(scope) WHERE state IN ('started', 'running')"
            )

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        receipt = result.pop("receipt_json")
        result["receipt"] = json.loads(receipt) if receipt else None
        return result

    def _one(self, connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM formal_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise FormalJobError("unknown formal job")
        return cast(sqlite3.Row, row)

    def start(
        self, *, scope: str, candidate: str, tier_digest: str, resource_class: str
    ) -> dict[str, Any]:
        """Create or return a job, superseding a distinct active candidate atomically."""
        if not scope:
            raise FormalJobError("formal-job scope is required")
        candidate, tier_digest, resource_class = (
            _candidate(candidate),
            _digest(tier_digest),
            _resource_class(resource_class),
        )
        now = time.time()
        with self._guard, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM formal_jobs WHERE scope=? AND candidate=? AND tier_digest=? "
                "AND resource_class=?",
                (scope, candidate, tier_digest, resource_class),
            ).fetchone()
            if existing is not None:
                connection.execute("COMMIT")
                return self._record(existing)
            active = connection.execute(
                "SELECT * FROM formal_jobs WHERE scope=? AND state IN ('started', 'running')",
                (scope,),
            ).fetchall()
            for row in active:
                receipt = self._terminal_receipt(row, "superseded", now, None, "candidate-replaced")
                connection.execute(
                    "UPDATE formal_jobs SET state='superseded', revision=revision+1, updated=?, "
                    "execution_token=NULL, receipt_json=? WHERE job_id=?",
                    (now, json.dumps(receipt, sort_keys=True), row["job_id"]),
                )
            job_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO formal_jobs VALUES (?, ?, ?, ?, ?, 'started', 1, NULL, ?, ?, NULL)",
                (job_id, scope, candidate, tier_digest, resource_class, now, now),
            )
            row = self._one(connection, job_id)
            connection.execute("COMMIT")
            return self._record(row)

    def begin(self, job_id: str, *, expected_revision: int) -> dict[str, Any]:
        """Claim a short execution token; no database transaction survives this call."""
        token = uuid.uuid4().hex
        now = time.time()
        with self._guard, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._one(connection, job_id)
            if row["state"] != "started" or row["revision"] != expected_revision:
                connection.execute("ROLLBACK")
                raise FormalJobError("formal job is not startable at the expected revision")
            connection.execute(
                "UPDATE formal_jobs SET state='running', revision=revision+1, "
                "execution_token=?, updated=? "
                "WHERE job_id=?",
                (token, now, job_id),
            )
            updated = self._one(connection, job_id)
            connection.execute("COMMIT")
            result = self._record(updated)
            result["execution_token"] = token
            return result

    def recover(self, job_id: str, *, expected_revision: int) -> dict[str, Any]:
        """Fence a dead worker and make its candidate-bound job runnable again."""
        now = time.time()
        with self._guard, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._one(connection, job_id)
            if row["state"] != "running" or row["revision"] != expected_revision:
                connection.execute("ROLLBACK")
                raise FormalJobError("formal job is not recoverable at the expected revision")
            connection.execute(
                "UPDATE formal_jobs SET state='started', revision=revision+1, "
                "execution_token=NULL, updated=? "
                "WHERE job_id=?",
                (now, job_id),
            )
            updated = self._one(connection, job_id)
            connection.execute("COMMIT")
            return self._record(updated)

    def _terminal_receipt(
        self, row: sqlite3.Row, state: str, ended: float, exit_code: int | None, detail: str
    ) -> dict[str, Any]:
        return {
            "candidate": row["candidate"],
            "detail": detail,
            "ended": ended,
            "exit_code": exit_code,
            "job_id": row["job_id"],
            "resource_class": row["resource_class"],
            "state": state,
            "tier_digest": row["tier_digest"],
        }

    def finish(
        self,
        job_id: str,
        *,
        expected_revision: int,
        execution_token: str,
        state: str,
        exit_code: int | None,
        detail: str,
    ) -> dict[str, Any]:
        """Persist one terminal receipt only for the exact active execution token."""
        if state not in TERMINAL_STATES - {"superseded"}:
            raise FormalJobError("invalid terminal formal-job state")
        now = time.time()
        with self._guard, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._one(connection, job_id)
            if (
                row["state"] != "running"
                or row["revision"] != expected_revision
                or row["execution_token"] != execution_token
            ):
                connection.execute("ROLLBACK")
                raise FormalJobError("stale or fenced formal-job completion")
            receipt = self._terminal_receipt(row, state, now, exit_code, detail)
            connection.execute(
                "UPDATE formal_jobs SET state=?, revision=revision+1, "
                "execution_token=NULL, updated=?, "
                "receipt_json=? WHERE job_id=?",
                (state, now, json.dumps(receipt, sort_keys=True), job_id),
            )
            updated = self._one(connection, job_id)
            connection.execute("COMMIT")
            return self._record(updated)

    def execute(
        self, job_id: str, *, expected_revision: int, runner: Callable[[], tuple[int, str]]
    ) -> dict[str, Any]:
        """Run outside receipt authority, then atomically write its terminal classification."""
        running = self.begin(job_id, expected_revision=expected_revision)
        try:
            exit_code, detail = runner()
        except BaseException as error:
            return self.finish(
                job_id,
                expected_revision=running["revision"],
                execution_token=running["execution_token"],
                state="failed",
                exit_code=None,
                detail=type(error).__name__,
            )
        return self.finish(
            job_id,
            expected_revision=running["revision"],
            execution_token=running["execution_token"],
            state="completed" if exit_code == 0 else "failed",
            exit_code=exit_code,
            detail=detail,
        )

    def receipt(self, job_id: str, *, candidate: str, tier_digest: str) -> dict[str, Any]:
        """Return only a completed receipt matching the candidate and tier contract."""
        candidate, tier_digest = _candidate(candidate), _digest(tier_digest)
        with self._connect() as connection:
            row = self._one(connection, job_id)
            result = self._record(row)
        receipt = result["receipt"]
        if (
            result["state"] != "completed"
            or receipt is None
            or receipt["candidate"] != candidate
            or receipt["tier_digest"] != tier_digest
            or receipt["exit_code"] != 0
        ):
            raise FormalJobError("receipt is incomplete, stale, failed, or for another candidate")
        return cast(dict[str, Any], receipt)

    def status(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            return self._record(self._one(connection, job_id))


def tier_contract_digest(contract: Mapping[str, object]) -> str:
    """Produce a deterministic digest for the declared tier/resource contract."""
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
