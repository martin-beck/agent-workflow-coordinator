# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Durable SQLite control-plane records for upgrade barriers.

The control store is deliberately separate from the coordinator authority.  It
is the source of truth for rollback-context rechecks; Git backends do not have
an implementation yet and must fail closed.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from typing import cast

from tools.handoffctl import locked

SCHEMA_VERSION = 1
IDENTITY_FIELDS = (
    "operation_id",
    "project_id",
    "state_revision",
    "fencing_token",
    "fencing_owner",
    "backend",
    "authority_revision",
    "durable_barrier_id",
    "barrier_identity_digest",
    "envelope_digest",
    "target",
)
STATUSES = {"held", "releasing", "released", "ambiguous"}
STATUS_TRANSITIONS = {
    "held": {"held", "releasing", "ambiguous"},
    "releasing": {"releasing", "released", "ambiguous"},
    "released": {"released", "ambiguous"},
    "ambiguous": {"ambiguous"},
}
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_INSERT_SQL = (
    "INSERT INTO barrier (operation_id,project_id,state_revision,fencing_token,fencing_owner,"
    "backend,authority_revision,durable_barrier_id,barrier_identity_digest,envelope_digest,"
    "target,status,revision) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
_UPDATE_SQL = (
    "UPDATE barrier SET project_id=?,state_revision=?,fencing_token=?,fencing_owner=?,backend=?,"
    "authority_revision=?,durable_barrier_id=?,barrier_identity_digest=?,envelope_digest=?,"
    "target=?,status=?,revision=? WHERE operation_id=? AND revision=?"
)


class ControlStoreError(RuntimeError):
    """Control-store data is unavailable or failed validation."""


def _validate(record: Mapping[str, object]) -> dict[str, object]:  # noqa: C901
    if set(record) != set(IDENTITY_FIELDS) | {"status", "revision"}:
        raise ControlStoreError("control record fields are invalid")
    if not isinstance(record["state_revision"], int) or isinstance(record["state_revision"], bool):
        raise ControlStoreError("control state revision is invalid")
    if (
        record["state_revision"] < 1
        or not isinstance(record["revision"], int)
        or isinstance(record["revision"], bool)
        or record["revision"] < 1
    ):
        raise ControlStoreError("control revision is invalid")
    try:
        project = uuid.UUID(str(record["project_id"]))
    except ValueError as error:
        raise ControlStoreError("control project_id is invalid") from error
    if project.version != 4:
        raise ControlStoreError("control project_id must be UUIDv4")
    if record["backend"] != "sqlite" or record["target"] not in {"new", "rollback"}:
        raise ControlStoreError("control backend or target is invalid")
    for field in (
        "operation_id",
        "fencing_token",
        "fencing_owner",
        "authority_revision",
        "durable_barrier_id",
    ):
        value = record[field]
        if not isinstance(value, str) or not _TOKEN.fullmatch(value):
            raise ControlStoreError(f"control {field} is invalid")
    for field in ("barrier_identity_digest", "envelope_digest"):
        value = record[field]
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            raise ControlStoreError(f"control {field} is invalid")
    if record["status"] not in STATUSES:
        raise ControlStoreError("control status is invalid")
    return dict(record)


class SQLiteRollbackControlStore:
    """WAL-backed control store with coordinator-common locking and CAS."""

    def __init__(self, path: Path, project_id: str) -> None:
        self.path = path
        self.project_id = project_id
        try:
            project = uuid.UUID(project_id)
        except ValueError as error:
            raise ControlStoreError("control project_id must be UUIDv4") from error
        if project.version != 4:
            raise ControlStoreError("control project_id must be UUIDv4")

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        try:
            mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            if mode != "wal":
                raise ControlStoreError("control store WAL is unavailable")
            connection.execute("PRAGMA synchronous=FULL")
            if int(connection.execute("PRAGMA synchronous").fetchone()[0]) != 2:
                raise ControlStoreError("control store FULL durability is unavailable")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS control_meta "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS barrier (
                    operation_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    state_revision INTEGER NOT NULL,
                    fencing_token TEXT NOT NULL,
                    fencing_owner TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    authority_revision TEXT NOT NULL,
                    durable_barrier_id TEXT NOT NULL,
                    barrier_identity_digest TEXT NOT NULL,
                    envelope_digest TEXT NOT NULL,
                    target TEXT NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL
                )"""
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS one_active_barrier_per_project "
                "ON barrier(project_id) WHERE status IN ('held','releasing')"
            )
            connection.execute(
                "INSERT OR IGNORE INTO control_meta(key,value) VALUES ('schema_version','1')"
            )
            value = connection.execute(
                "SELECT value FROM control_meta WHERE key='project_id'"
            ).fetchone()
            if value is None:
                connection.execute(
                    "INSERT INTO control_meta(key,value) VALUES ('project_id',?)",
                    (self.project_id,),
                )
            elif value[0] != self.project_id:
                raise ControlStoreError("control store project binding mismatch")
            return connection
        except Exception:
            connection.close()
            raise

    def snapshot(self, operation_id: str) -> dict[str, object]:
        with locked(), closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT operation_id,project_id,state_revision,fencing_token,fencing_owner,backend,"
                "authority_revision,durable_barrier_id,barrier_identity_digest,envelope_digest,"
                "target,status,revision FROM barrier WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise ControlStoreError("control barrier is missing")
            return _validate(dict(zip((*IDENTITY_FIELDS, "status", "revision"), row, strict=True)))

    def verify_rollback_context(self, context: Mapping[str, object]) -> dict[str, object] | None:
        """Re-read the durable control record and compare every bound identity."""
        operation_id = context.get("operation_id")
        if not isinstance(operation_id, str):
            return None
        try:
            durable = self.snapshot(operation_id)
            supplied = _validate(
                {
                    **{field: context.get(field) for field in IDENTITY_FIELDS},
                    "status": durable["status"],
                    "revision": durable["revision"],
                }
            )
        except (ControlStoreError, TypeError):
            return None
        return durable if supplied == durable and supplied["target"] == "rollback" else None

    def cas(self, expected_revision: int, record: Mapping[str, object]) -> dict[str, object]:
        supplied = _validate(record)
        if supplied["project_id"] != self.project_id:
            raise ControlStoreError("control project binding mismatch")
        with locked(), closing(self._connect()) as connection:
            return self._cas_connection(connection, expected_revision, supplied)

    def _cas_connection(  # noqa: C901
        self, connection: sqlite3.Connection, expected_revision: int, supplied: dict[str, object]
    ) -> dict[str, object]:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT operation_id,project_id,state_revision,fencing_token,fencing_owner,backend,"
            "authority_revision,durable_barrier_id,barrier_identity_digest,envelope_digest,"
            "target,status,revision FROM barrier WHERE operation_id=?",
            (supplied["operation_id"],),
        ).fetchone()
        if current is not None and current[-1] != expected_revision:
            connection.rollback()
            raise ControlStoreError("control barrier CAS conflict")
        if current is None and expected_revision != 0:
            connection.rollback()
            raise ControlStoreError("control barrier does not exist")
        supplied["revision"] = expected_revision + 1
        if current is None and supplied["status"] != "held":
            connection.rollback()
            raise ControlStoreError("new control barrier must start held")
        if current is None:
            active = connection.execute(
                "SELECT operation_id FROM barrier WHERE project_id=? "
                "AND status IN ('held','releasing') LIMIT 1",
                (supplied["project_id"],),
            ).fetchone()
            if active is not None:
                connection.rollback()
                raise ControlStoreError("project already has an active barrier")
        if current is not None:
            current_record = dict(
                zip((*IDENTITY_FIELDS, "status", "revision"), current, strict=True)
            )
            if any(current_record[field] != supplied[field] for field in IDENTITY_FIELDS):
                connection.rollback()
                raise ControlStoreError("control identity changed during CAS")
        if current is not None and supplied["status"] not in STATUS_TRANSITIONS[str(current[-2])]:
            connection.rollback()
            raise ControlStoreError("illegal control barrier transition")
        columns = (*IDENTITY_FIELDS, "status", "revision")
        values = tuple(supplied[field] for field in columns)
        if current is None:
            connection.execute(_INSERT_SQL, values)
        else:
            connection.execute(
                _UPDATE_SQL,
                (
                    *(supplied[field] for field in columns if field != "operation_id"),
                    supplied["operation_id"],
                    expected_revision,
                ),
            )
        connection.commit()
        return dict(supplied)

    def with_barrier(
        self,
        expected_revision: int,
        record: Mapping[str, object],
        authority: Callable[[Mapping[str, object]], Mapping[str, object]],
    ) -> dict[str, object]:
        """Run one authority critical section while the coordinator lock is held.

        The initial ``held`` CAS is committed before the callback.  A callback
        failure therefore leaves a durable held barrier for explicit recovery.
        """
        supplied = _validate(record)
        if supplied["project_id"] != self.project_id:
            raise ControlStoreError("control project binding mismatch")
        with locked(), closing(self._connect()) as connection:
            held = self._cas_connection(connection, expected_revision, supplied)
            result = dict(authority(dict(held)))
            return self._cas_connection(connection, cast(int, held["revision"]), _validate(result))
