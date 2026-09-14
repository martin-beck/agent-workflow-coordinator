# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Durable SQLite control-plane records for upgrade barriers.

The control store is deliberately separate from the coordinator authority.  It
is the source of truth for rollback-context rechecks; Git backends do not have
an implementation yet and must fail closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, closing, contextmanager
from pathlib import Path
from typing import Protocol, cast

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
    "ambiguous": set(),
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


def canonical_barrier_digest(record: Mapping[str, object]) -> str:
    """Return the stable identity digest, excluding mutable status/revision and digests."""
    payload = {
        field: record[field]
        for field in IDENTITY_FIELDS
        if field not in {"barrier_identity_digest", "envelope_digest"}
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def canonical_envelope_digest(record: Mapping[str, object]) -> str:
    """Return the stable full identity envelope digest."""
    payload = {field: record[field] for field in IDENTITY_FIELDS if field != "envelope_digest"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class ControlStoreError(RuntimeError):
    """Control-store data is unavailable or failed validation."""


class UpgradeAdapter(Protocol):
    """Minimal engine adapter surface wrapped by the control store."""

    def snapshot(self, phase: str, context: Mapping[str, object]) -> Mapping[str, object]: ...

    def execute(self, phase: str, context: Mapping[str, object]) -> Mapping[str, object]: ...


class SQLiteControlStoreAdapter:
    """Bind an engine adapter's rollback authority to a durable SQLite store."""

    def __init__(self, delegate: UpgradeAdapter, store: SQLiteRollbackControlStore) -> None:
        self._delegate = delegate
        self._store = store

    def snapshot(self, phase: str, context: Mapping[str, object]) -> Mapping[str, object]:
        return self._delegate.snapshot(phase, context)

    def execute(self, phase: str, context: Mapping[str, object]) -> Mapping[str, object]:
        return self._delegate.execute(phase, context)

    def verify_rollback_context(self, context: Mapping[str, object]) -> dict[str, object] | None:
        if self._store.operation_owned_by_current_thread:
            return self._store._verify_rollback_context_locked(context)
        return self._store.verify_rollback_context(context)

    def release_rollback_context(self, context: Mapping[str, object]) -> Mapping[str, object]:
        operation_id = str(context["operation_id"])
        if self._store.operation_owned_by_current_thread:
            return self._store._release_locked(operation_id)
        return self._store.release(operation_id)

    def revalidate_rollback(
        self, context: Mapping[str, object], result: Mapping[str, object]
    ) -> Mapping[str, object]:
        durable = (
            self._store._verify_rollback_context_locked(context)
            if self._store.operation_owned_by_current_thread
            else self._store.verify_rollback_context(context)
        )
        if durable is None or durable["status"] != "released":
            raise ControlStoreError("rollback control record is not released")
        return dict(result)

    def operation_lock(self) -> AbstractContextManager[None]:
        return self._store.operation_lock()


def bind_control_store(
    backend: str, delegate: UpgradeAdapter, store: SQLiteRollbackControlStore | None
) -> SQLiteControlStoreAdapter:
    """Construct only a proven SQLite adapter; Git is explicitly fail-closed."""
    if backend != "sqlite" or store is None or store.authority_path is None:
        raise ControlStoreError(
            "durable rollback control store or authority binding is unavailable"
        )
    return SQLiteControlStoreAdapter(delegate, store)


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
    if record["barrier_identity_digest"] != canonical_barrier_digest(record):
        raise ControlStoreError("control barrier identity digest is invalid")
    if record["envelope_digest"] != canonical_envelope_digest(record):
        raise ControlStoreError("control envelope digest is invalid")
    if record["status"] not in STATUSES:
        raise ControlStoreError("control status is invalid")
    return dict(record)


class SQLiteRollbackControlStore:
    """WAL-backed control store with coordinator-common locking and CAS."""

    def __init__(self, path: Path, project_id: str, authority_path: Path | None = None) -> None:
        self._check_paths(path, authority_path)
        self.path = path
        self.authority_path = authority_path
        self._operation_owner: int | None = None
        self.project_id = project_id
        try:
            project = uuid.UUID(project_id)
        except ValueError as error:
            raise ControlStoreError("control project_id must be UUIDv4") from error
        if project.version != 4:
            raise ControlStoreError("control project_id must be UUIDv4")

    @staticmethod
    def _check_paths(path: Path, authority_path: Path | None) -> None:  # noqa: C901
        if path.exists() and path.is_symlink():
            raise ControlStoreError("control store path must not be a symlink")
        if any(parent.exists() and parent.is_symlink() for parent in path.parents):
            raise ControlStoreError("control store parent must not be a symlink")
        if path.exists():
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    if not os.fstat(descriptor).st_mode & 0o100000:
                        raise ControlStoreError("control store is not a regular file")
                finally:
                    os.close(descriptor)
            except OSError as error:
                raise ControlStoreError("control store descriptor is unsafe") from error
        if authority_path is None:
            return
        if authority_path.exists() and authority_path.is_symlink():
            raise ControlStoreError("authority path must not be a symlink")
        if authority_path.exists():
            try:
                descriptor = os.open(authority_path, os.O_RDONLY | os.O_NOFOLLOW)
                os.close(descriptor)
            except OSError as error:
                raise ControlStoreError("authority descriptor is unsafe") from error
        try:
            if path.exists() and authority_path.exists() and path.samefile(authority_path):
                raise ControlStoreError("control store aliases authority")
        except OSError as error:
            raise ControlStoreError("control and authority identity is unavailable") from error

    def _connect(self) -> sqlite3.Connection:
        self._check_paths(self.path, self.authority_path)
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
            schema = connection.execute(
                "SELECT value FROM control_meta WHERE key='schema_version'"
            ).fetchone()
            if schema is None or schema[0] != str(SCHEMA_VERSION):
                raise ControlStoreError("control store schema version mismatch")
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

    @contextmanager
    def _control_lock(self) -> Iterator[None]:
        """Hold the store-specific lock after the coordinator-common lock."""
        lock_path = self.path.parent / f".{self.path.name}.lock"
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
        except OSError as error:
            raise ControlStoreError("control store lock is unavailable") from error
        try:
            import fcntl
        except ImportError as error:  # pragma: no cover - coordinator is POSIX-only
            os.close(descriptor)
            raise ControlStoreError("control store locking is unavailable") from error
        try:
            deadline = time.monotonic() + 10.0
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as error:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ControlStoreError(
                            "control store lock acquisition timed out"
                        ) from error
                    time.sleep(min(0.05, remaining))
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    @contextmanager
    def operation_lock(self) -> Iterator[None]:
        """Hold common then control-store locks once for a complete operation."""
        if self._operation_owner == threading.get_ident():
            raise ControlStoreError("control store lock is non-reentrant")
        with locked(), self._control_lock():
            if self._operation_owner is not None:
                raise ControlStoreError("control store operation is already active")
            self._operation_owner = threading.get_ident()
            try:
                yield
            finally:
                self._operation_owner = None

    @property
    def operation_owned_by_current_thread(self) -> bool:
        return self._operation_owner == threading.get_ident()

    def _require_operation_lock(self) -> None:
        if not self.operation_owned_by_current_thread:
            raise ControlStoreError("control store operation lock is required")

    def snapshot(self, operation_id: str) -> dict[str, object]:
        if self._operation_owner is not None:
            raise ControlStoreError("control store lock is non-reentrant")
        with self.operation_lock():
            return self._snapshot_locked(operation_id)

    def _snapshot_locked(self, operation_id: str) -> dict[str, object]:
        self._require_operation_lock()
        with closing(self._connect()) as connection:
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
        if self._operation_owner is not None:
            raise ControlStoreError("control store lock is non-reentrant")
        with self.operation_lock():
            return self._verify_rollback_context_locked(context)

    def _verify_rollback_context_locked(
        self, context: Mapping[str, object]
    ) -> dict[str, object] | None:
        self._require_operation_lock()
        operation_id = context.get("operation_id")
        if not isinstance(operation_id, str):
            return None
        try:
            durable = self._snapshot_locked(operation_id)
            supplied = _validate(
                {
                    **{field: context.get(field) for field in IDENTITY_FIELDS},
                    "status": durable["status"],
                    "revision": durable["revision"],
                }
            )
        except (ControlStoreError, TypeError):
            return None
        return (
            durable
            if supplied == durable
            and supplied["target"] == "rollback"
            and durable["status"] in {"held", "releasing", "released"}
            else None
        )

    def cas(self, expected_revision: int, record: Mapping[str, object]) -> dict[str, object]:
        supplied = _validate(record)
        if supplied["project_id"] != self.project_id:
            raise ControlStoreError("control project binding mismatch")
        if self._operation_owner is not None:
            raise ControlStoreError("control store lock is non-reentrant")
        with self.operation_lock():
            return self._cas_locked(expected_revision, supplied)

    def _cas_locked(self, expected_revision: int, supplied: dict[str, object]) -> dict[str, object]:
        self._require_operation_lock()
        with closing(self._connect()) as connection:
            return self._cas_connection(connection, expected_revision, supplied)

    def release(self, operation_id: str) -> dict[str, object]:
        """Durably release a held barrier through the releasing state."""
        if self._operation_owner is not None:
            raise ControlStoreError("control store lock is non-reentrant")
        with self.operation_lock():
            return self._release_locked(operation_id)

    def _release_locked(self, operation_id: str) -> dict[str, object]:
        self._require_operation_lock()
        current = self._snapshot_locked(operation_id)
        if current["status"] != "held":
            raise ControlStoreError("barrier is not held")
        releasing = self._cas_locked(
            cast(int, current["revision"]), {**current, "status": "releasing"}
        )
        return self._cas_locked(
            cast(int, releasing["revision"]), {**releasing, "status": "released"}
        )

    def reconcile_release(self, operation_id: str) -> dict[str, object]:
        """Complete a release interrupted after its durable releasing transition."""
        current = self.snapshot(operation_id)
        if current["status"] != "releasing":
            raise ControlStoreError("barrier is not awaiting release reconciliation")
        return self.cas(cast(int, current["revision"]), {**current, "status": "released"})

    def reconcile_ambiguous(
        self, operation_id: str, replacement: Mapping[str, object]
    ) -> dict[str, object]:
        """Start a new fenced operation only after explicit ambiguous recovery."""
        previous = self.snapshot(operation_id)
        if previous["status"] != "ambiguous":
            raise ControlStoreError("only ambiguous barriers require reconciliation")
        candidate = _validate(replacement)
        if candidate["operation_id"] == operation_id or candidate["status"] != "held":
            raise ControlStoreError("ambiguous reconciliation requires a new held operation")
        if candidate["project_id"] != previous["project_id"] or cast(
            int, candidate["state_revision"]
        ) <= cast(int, previous["state_revision"]):
            raise ControlStoreError("ambiguous reconciliation requires a newer project fence")
        return self.cas(0, candidate)

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
            latest = connection.execute(
                "SELECT MAX(state_revision) FROM barrier WHERE project_id=?",
                (supplied["project_id"],),
            ).fetchone()[0]
            if latest is not None and supplied["state_revision"] <= latest:
                connection.rollback()
                raise ControlStoreError("stale control state revision")
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
        if self._operation_owner is not None:
            raise ControlStoreError("control store lock is non-reentrant")
        with self.operation_lock(), closing(self._connect()) as connection:
            held = self._cas_connection(connection, expected_revision, supplied)
            result = dict(authority(dict(held)))
            return self._cas_connection(connection, cast(int, held["revision"]), _validate(result))
