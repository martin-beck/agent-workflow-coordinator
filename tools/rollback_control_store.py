# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Durable SQLite control-plane records for upgrade barriers.

The control store is deliberately separate from the coordinator authority.  It
is the source of truth for rollback-context rechecks; Git backends do not have
an implementation yet and must fail closed.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from tools.handoffctl import locked
from tools.upgrade_identity import (
    ENVELOPE_FIELDS,
    UpgradeIdentityError,
    validate_envelope,
)

SCHEMA_VERSION = 2
IDENTITY_FIELDS = ENVELOPE_FIELDS
STATUSES = {"held", "releasing", "released", "ambiguous"}
STATUS_TRANSITIONS = {
    "held": {"held", "releasing", "ambiguous"},
    "releasing": {"releasing", "released", "ambiguous"},
    "released": {"released"},
    "ambiguous": set(),
}
_COLUMNS = (*IDENTITY_FIELDS, "status", "revision")
_SELECT_COLUMNS = (
    "schema_version,backend,project_id,operation_id,state_revision,authority_revision,"
    "fencing_token,fencing_owner,durable_barrier_id,artifact_root,source,destination,manifest,"
    "barrier_identity_digest,target,envelope_digest,status,revision"
)
_SELECT_SQL = f"SELECT {_SELECT_COLUMNS} FROM barrier WHERE operation_id=?"  # noqa: S608
_INSERT_SQL = (
    "INSERT INTO barrier (schema_version,backend,project_id,operation_id,state_revision,"
    "authority_revision,fencing_token,fencing_owner,durable_barrier_id,artifact_root,source,"
    "destination,manifest,barrier_identity_digest,target,envelope_digest,status,revision) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
_UPDATE_FIELDS = tuple(field for field in _COLUMNS if field != "operation_id")
_UPDATE_SQL = (
    "UPDATE barrier SET schema_version=?,backend=?,project_id=?,state_revision=?,"
    "authority_revision=?,fencing_token=?,fencing_owner=?,durable_barrier_id=?,artifact_root=?,"
    "source=?,destination=?,manifest=?,barrier_identity_digest=?,target=?,envelope_digest=?,"
    "status=?,revision=? "
    "WHERE operation_id=? AND revision=?"
)
_RELEASE_EVIDENCE_FIELDS = {
    "restored_verified",
    "runtime_validated",
    "backend_roundtrip_valid",
    "backend",
    "fencing_token",
}


@dataclass(frozen=True)
class _ReleaseAuthorization:
    operation_id: str
    envelope_digest: str
    fencing_token: str
    revision: int


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
        self._release_authorization: _ReleaseAuthorization | None = None

    def snapshot(self, phase: str, context: Mapping[str, object]) -> Mapping[str, object]:
        return self._delegate.snapshot(phase, context)

    def execute(self, phase: str, context: Mapping[str, object]) -> Mapping[str, object]:
        return self._delegate.execute(phase, context)

    def verify_rollback_context(self, context: Mapping[str, object]) -> dict[str, object] | None:
        if self._store.operation_owned_by_current_thread:
            return self._store._verify_rollback_context_locked(context)
        return self._store.verify_rollback_context(context)

    def begin_release_rollback_context(self, context: Mapping[str, object]) -> Mapping[str, object]:
        operation_id = str(context["operation_id"])
        if not self._store.operation_owned_by_current_thread:
            raise ControlStoreError("rollback release requires the outer operation lock")
        self._release_authorization = None
        return self._store._begin_release_locked(operation_id)

    def complete_release_rollback_context(
        self, context: Mapping[str, object]
    ) -> Mapping[str, object]:
        operation_id = str(context["operation_id"])
        if not self._store.operation_owned_by_current_thread:
            raise ControlStoreError("rollback release requires the outer operation lock")
        authorization = self._release_authorization
        if authorization is None:
            raise ControlStoreError("rollback release lacks authority revalidation")
        try:
            return self._store._complete_release_locked(operation_id, authorization)
        finally:
            self._release_authorization = None

    def revalidate_rollback(
        self, context: Mapping[str, object], result: Mapping[str, object]
    ) -> Mapping[str, object]:
        durable = (
            self._store._verify_rollback_context_locked(context)
            if self._store.operation_owned_by_current_thread
            else self._store.verify_rollback_context(context)
        )
        if durable is None or durable["status"] not in {"releasing", "released"}:
            raise ControlStoreError("rollback control record is not ready for reopen validation")
        verifier = getattr(self._delegate, "revalidate_rollback", None)
        if not callable(verifier):
            raise ControlStoreError("authority rollback revalidation is unavailable")
        evidence = verifier(context, result)
        if not isinstance(evidence, Mapping):
            raise ControlStoreError("authority rollback revalidation is invalid")
        if durable["status"] == "releasing":
            if not self._store.operation_owned_by_current_thread:
                raise ControlStoreError("rollback revalidation requires the outer operation lock")
            self._release_authorization = self._store._authorize_release_locked(context, evidence)
        return dict(evidence)

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


def _validate(record: Mapping[str, object]) -> dict[str, object]:
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
        validate_envelope({field: record[field] for field in IDENTITY_FIELDS})
    except UpgradeIdentityError as error:
        raise ControlStoreError("control envelope identity is invalid") from error
    if record["backend"] != "sqlite":
        raise ControlStoreError("control backend is invalid")
    if record["status"] not in STATUSES:
        raise ControlStoreError("control status is invalid")
    return dict(record)


def _validate_expected_revision(expected_revision: object) -> int:
    if type(expected_revision) is not int or expected_revision < 0:
        raise ControlStoreError("control expected revision is invalid")
    return expected_revision


class SQLiteRollbackControlStore:
    """WAL-backed control store with coordinator-common locking and CAS."""

    def __init__(self, path: Path, project_id: str, authority_path: Path | None = None) -> None:
        try:
            project = uuid.UUID(project_id)
        except ValueError as error:
            raise ControlStoreError("control project_id must be UUIDv4") from error
        if project.version != 4:
            raise ControlStoreError("control project_id must be UUIDv4")
        self.path = path.absolute()
        self.authority_path = authority_path.absolute() if authority_path is not None else None
        if self.authority_path == self.path:
            raise ControlStoreError("control store aliases authority")
        self._authority_identity = (
            self._existing_regular_identity(self.authority_path)
            if self.authority_path is not None
            else None
        )
        self._parent_identity, self._control_identity = self._prepare_regular_file(self.path)
        if self._authority_identity == self._control_identity:
            raise ControlStoreError("control store aliases authority")
        self._lock_path = self.path.parent / f".{self.path.name}.lock"
        lock_parent, self._lock_identity = self._prepare_regular_file(self._lock_path)
        if lock_parent != self._parent_identity:
            raise ControlStoreError("control store lock parent identity changed")
        self._operation_owner: int | None = None
        self.project_id = project_id

    @staticmethod
    def _file_identity(value: os.stat_result) -> tuple[int, int]:
        return value.st_dev, value.st_ino

    @classmethod
    def _open_parent(cls, path: Path) -> tuple[int, tuple[int, int]]:
        if not path.is_absolute() or path.name in {"", ".", ".."}:
            raise ControlStoreError("control store path is invalid")
        descriptor = -1
        try:
            descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
            for component in path.parent.parts[1:]:
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                previous = descriptor
                descriptor = child
                os.close(previous)
        except OSError as error:
            if descriptor >= 0:
                os.close(descriptor)
            raise ControlStoreError("control store parent descriptor is unsafe") from error
        return descriptor, cls._file_identity(os.fstat(descriptor))

    @classmethod
    def _prepare_regular_file(cls, path: Path) -> tuple[tuple[int, int], tuple[int, int]]:
        parent, parent_identity = cls._open_parent(path)
        created = False
        try:
            try:
                descriptor = os.open(path.name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=parent)
            except FileNotFoundError:
                try:
                    descriptor = os.open(
                        path.name,
                        os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=parent,
                    )
                    created = True
                except OSError as error:
                    raise ControlStoreError("control store creation is unsafe") from error
            except OSError as error:
                raise ControlStoreError("control store descriptor is unsafe") from error
            try:
                status = os.fstat(descriptor)
                if not stat.S_ISREG(status.st_mode):
                    raise ControlStoreError("control store is not a regular file")
                if created:
                    os.fsync(descriptor)
                    os.fsync(parent)
                return parent_identity, cls._file_identity(status)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)

    @classmethod
    def _existing_regular_identity(cls, path: Path) -> tuple[int, int]:
        parent, _ = cls._open_parent(path)
        try:
            try:
                descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            except OSError as error:
                raise ControlStoreError("authority descriptor is unsafe") from error
            try:
                status = os.fstat(descriptor)
                if not stat.S_ISREG(status.st_mode):
                    raise ControlStoreError("authority is not a regular file")
                return cls._file_identity(status)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)

    def _open_bound_file(
        self, path: Path, expected_parent: tuple[int, int], expected_file: tuple[int, int]
    ) -> tuple[int, int]:
        parent, parent_identity = self._open_parent(path)
        if parent_identity != expected_parent:
            os.close(parent)
            raise ControlStoreError("control store parent identity changed")
        try:
            descriptor = os.open(path.name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=parent)
        except OSError as error:
            os.close(parent)
            raise ControlStoreError("control store descriptor is unsafe") from error
        try:
            status = os.fstat(descriptor)
        except OSError as error:
            os.close(descriptor)
            os.close(parent)
            raise ControlStoreError("control store descriptor is unreadable") from error
        if not stat.S_ISREG(status.st_mode) or self._file_identity(status) != expected_file:
            os.close(descriptor)
            os.close(parent)
            raise ControlStoreError("control store descriptor identity changed")
        return parent, descriptor

    def _recheck_authority(self) -> None:
        if self.authority_path is None or self._authority_identity is None:
            return
        current = self._existing_regular_identity(self.authority_path)
        if current != self._authority_identity or current == self._control_identity:
            raise ControlStoreError("authority descriptor identity changed")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self._recheck_authority()
        parent, descriptor = self._open_bound_file(
            self.path, self._parent_identity, self._control_identity
        )
        try:
            connection = sqlite3.connect(
                f"file:/proc/self/fd/{descriptor}?mode=rw",
                isolation_level=None,
                timeout=10,
                uri=True,
            )
        except Exception:
            os.close(descriptor)
            os.close(parent)
            raise
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
                    schema_version INTEGER NOT NULL,
                    backend TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    operation_id TEXT PRIMARY KEY,
                    state_revision INTEGER NOT NULL,
                    authority_revision TEXT NOT NULL,
                    fencing_token TEXT NOT NULL,
                    fencing_owner TEXT NOT NULL,
                    durable_barrier_id TEXT NOT NULL,
                    artifact_root TEXT NOT NULL,
                    source TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    manifest TEXT NOT NULL,
                    barrier_identity_digest TEXT NOT NULL,
                    target TEXT NOT NULL,
                    envelope_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL
                )"""
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS one_active_barrier_per_project "
                "ON barrier(project_id) WHERE status IN ('held','releasing')"
            )
            connection.execute(
                "INSERT OR IGNORE INTO control_meta(key,value) VALUES ('schema_version',?)",
                (str(SCHEMA_VERSION),),
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
            yield connection
        except Exception:
            raise
        finally:
            connection.close()
            try:
                reopened_parent, reopened = self._open_bound_file(
                    self.path, self._parent_identity, self._control_identity
                )
                os.close(reopened)
                os.close(reopened_parent)
                self._recheck_authority()
            finally:
                os.close(descriptor)
                os.close(parent)

    @contextmanager
    def _control_lock(self) -> Iterator[None]:
        """Hold the store-specific lock after the coordinator-common lock."""
        parent, descriptor = self._open_bound_file(
            self._lock_path, self._parent_identity, self._lock_identity
        )
        try:
            import fcntl
        except ImportError as error:  # pragma: no cover - coordinator is POSIX-only
            os.close(descriptor)
            os.close(parent)
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
                os.close(parent)

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
        with self._connection() as connection:
            row = connection.execute(_SELECT_SQL, (operation_id,)).fetchone()
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
        expected_revision = _validate_expected_revision(expected_revision)
        supplied = _validate(record)
        if supplied["status"] == "released":
            raise ControlStoreError("released status requires authority revalidation")
        if supplied["project_id"] != self.project_id:
            raise ControlStoreError("control project binding mismatch")
        if self._operation_owner is not None:
            raise ControlStoreError("control store lock is non-reentrant")
        with self.operation_lock():
            return self._cas_locked(expected_revision, supplied)

    def _cas_locked(self, expected_revision: int, supplied: dict[str, object]) -> dict[str, object]:
        expected_revision = _validate_expected_revision(expected_revision)
        self._require_operation_lock()
        with self._connection() as connection:
            return self._cas_connection(connection, expected_revision, supplied)

    def begin_release(self, operation_id: str) -> dict[str, object]:
        """Commit the held-to-releasing transition without reopening authority."""
        if self._operation_owner is not None:
            raise ControlStoreError("control store lock is non-reentrant")
        with self.operation_lock():
            return self._begin_release_locked(operation_id)

    def _begin_release_locked(self, operation_id: str) -> dict[str, object]:
        self._require_operation_lock()
        current = self._snapshot_locked(operation_id)
        if current["status"] != "held":
            raise ControlStoreError("barrier is not held")
        return self._cas_locked(cast(int, current["revision"]), {**current, "status": "releasing"})

    def _authorize_release_locked(
        self, context: Mapping[str, object], evidence: Mapping[str, object]
    ) -> _ReleaseAuthorization:
        self._require_operation_lock()
        current = self._snapshot_locked(str(context.get("operation_id", "")))
        if current["status"] != "releasing" or any(
            current[field] != context.get(field) for field in IDENTITY_FIELDS
        ):
            raise ControlStoreError("release authorization identity changed")
        if (
            set(evidence) != _RELEASE_EVIDENCE_FIELDS
            or any(
                evidence.get(field) is not True
                for field in (
                    "restored_verified",
                    "runtime_validated",
                    "backend_roundtrip_valid",
                )
            )
            or evidence.get("backend") != current["backend"]
            or evidence.get("fencing_token") != current["fencing_token"]
        ):
            raise ControlStoreError("release authority evidence is invalid")
        return _ReleaseAuthorization(
            operation_id=str(current["operation_id"]),
            envelope_digest=str(current["envelope_digest"]),
            fencing_token=str(current["fencing_token"]),
            revision=cast(int, current["revision"]),
        )

    def _complete_release_locked(
        self, operation_id: str, authorization: _ReleaseAuthorization
    ) -> dict[str, object]:
        self._require_operation_lock()
        releasing = self._snapshot_locked(operation_id)
        if (
            releasing["status"] != "releasing"
            or authorization.operation_id != operation_id
            or authorization.envelope_digest != releasing["envelope_digest"]
            or authorization.fencing_token != releasing["fencing_token"]
            or authorization.revision != releasing["revision"]
        ):
            raise ControlStoreError("barrier release authorization is stale")
        return self._cas_locked(releasing["revision"], {**releasing, "status": "released"})

    def reconcile_release(self, operation_id: str) -> dict[str, object]:
        """Reject blind release; engine recovery must revalidate authority and journal."""
        raise ControlStoreError(
            f"release reconciliation for {operation_id!r} requires verified engine recovery"
        )

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
        expected_revision = _validate_expected_revision(expected_revision)
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(_SELECT_SQL, (supplied["operation_id"],)).fetchone()
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
        with self.operation_lock(), self._connection() as connection:
            held = self._cas_connection(connection, expected_revision, supplied)
            result = dict(authority(dict(held)))
            return self._cas_connection(connection, cast(int, held["revision"]), _validate(result))
