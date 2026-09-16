# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Durable SQLite control-plane records for upgrade barriers.

The control store is deliberately separate from the coordinator authority.  It
is the source of truth for rollback-context rechecks; Git backends do not have
an implementation yet and must fail closed.
"""

from __future__ import annotations

import json
import os
import re
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

from tools.handoffctl import (
    CoordinatorLockGuard,
    LockOwnershipError,
    coordinator_lock_path,
    locked,
)
from tools.rollback_evidence import BackupObservation
from tools.upgrade_authority import inspect_sqlite_release_authority
from tools.upgrade_identity import (
    ENVELOPE_FIELDS,
    BarrierChildIdentity,
    BarrierSessionIdentity,
    UpgradeIdentityError,
    validate_envelope,
)

SCHEMA_VERSION = 2
SIDECAR_SUFFIXES = ("-wal", "-shm")
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
_CAUSE_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


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


@dataclass(frozen=True, slots=True)
class SQLiteAuthorityRuntimeState:
    """Facts produced by a fresh authority and runtime reread."""

    backend: str
    project_id: str
    authority_revision: str
    fencing_token: str
    target: str
    integrity_check: str
    foreign_key_violations: int
    backend_roundtrip: str


@dataclass(frozen=True, slots=True)
class BarrierSessionState:
    """Typed, immutable view of a target-neutral barrier session.

    The existing SQLite rollback adapter remains v9 and fail-closed.  This
    value object is the v10 control-store seam: it makes the shared session
    identity and CAS state explicit without pretending that the durable
    adapter or SQLite mutation fencing is complete.
    """

    identity: BarrierSessionIdentity
    status: str
    revision: int
    forward_child: BarrierChildIdentity | None = None
    rollback_child: BarrierChildIdentity | None = None

    def __post_init__(self) -> None:
        if self.status not in {"held", "releasing", "released", "ambiguous"}:
            raise ControlStoreError("barrier session status is invalid")
        if type(self.revision) is not int or self.revision < 1:
            raise ControlStoreError("barrier session revision is invalid")
        for child in (self.forward_child, self.rollback_child):
            if child is not None:
                child.validate_for(self.identity)
        if (
            self.forward_child is not None
            and self.rollback_child is not None
            and self.forward_child.operation_id == self.rollback_child.operation_id
        ):
            raise ControlStoreError("barrier child operation identities must be distinct")


class BarrierSessionContract:
    """Small pure CAS contract used to gate a future durable adapter.

    This class intentionally has no filesystem or SQLite side effects.  It is
    suitable for exact transition tests while the production adapter remains
    disabled until AR-0012 supplies authority fencing and crash evidence.
    """

    def __init__(self, identity: BarrierSessionIdentity) -> None:
        self._state = BarrierSessionState(identity, "held", 1)

    @property
    def state(self) -> BarrierSessionState:
        return self._state

    def recheck_held(self, expected_revision: int) -> BarrierSessionState:
        self._expect(expected_revision, {"held"})
        return self._state

    def bind_child(
        self, expected_revision: int, child: BarrierChildIdentity
    ) -> BarrierSessionState:
        self._expect(expected_revision, {"held"})
        child.validate_for(self._state.identity)
        if child.target == "new":
            if self._state.forward_child is not None:
                raise ControlStoreError("forward barrier child is already bound")
            updated = BarrierSessionState(
                self._state.identity,
                self._state.status,
                self._state.revision + 1,
                child,
                self._state.rollback_child,
            )
        else:
            if self._state.forward_child is None:
                raise ControlStoreError("rollback child requires a bound forward child")
            if self._state.rollback_child is not None:
                raise ControlStoreError("rollback barrier child is already bound")
            updated = BarrierSessionState(
                self._state.identity,
                self._state.status,
                self._state.revision + 1,
                self._state.forward_child,
                child,
            )
        self._state = updated
        return updated

    def begin_reopen(self, expected_revision: int, child_target: str) -> BarrierSessionState:
        self._expect(expected_revision, {"held"})
        if child_target not in {"new", "rollback"}:
            raise ControlStoreError("reopen child target is invalid")
        child = (
            self._state.rollback_child if child_target == "rollback" else self._state.forward_child
        )
        if child is None:
            raise ControlStoreError("reopen child is not bound")
        self._state = BarrierSessionState(
            self._state.identity,
            "releasing",
            self._state.revision + 1,
            self._state.forward_child,
            self._state.rollback_child,
        )
        return self._state

    def complete_reopen(
        self, expected_revision: int, fresh_runtime_verified: bool
    ) -> BarrierSessionState:
        self._expect(expected_revision, {"releasing"})
        if fresh_runtime_verified is not True:
            raise ControlStoreError("fresh runtime evidence is required to release barrier")
        self._state = BarrierSessionState(
            self._state.identity,
            "released",
            self._state.revision + 1,
            self._state.forward_child,
            self._state.rollback_child,
        )
        return self._state

    def mark_ambiguous(self, expected_revision: int, cause_code: str) -> BarrierSessionState:
        self._expect(expected_revision, {"held", "releasing"})
        if _CAUSE_CODE.fullmatch(cause_code) is None:
            raise ControlStoreError("ambiguous barrier cause code is invalid")
        self._state = BarrierSessionState(
            self._state.identity,
            "ambiguous",
            self._state.revision + 1,
            self._state.forward_child,
            self._state.rollback_child,
        )
        return self._state

    def _expect(self, expected_revision: int, statuses: set[str]) -> None:
        if type(expected_revision) is not int or expected_revision != self._state.revision:
            raise ControlStoreError("barrier session revision conflict")
        if self._state.status not in statuses:
            raise ControlStoreError("barrier session transition is not permitted")


class AuthorityRuntimeRereader(Protocol):
    """Trusted boundary that rereads authority and runtime instead of echoing claims."""

    def reread_rollback(
        self, context: Mapping[str, object], result: Mapping[str, object]
    ) -> SQLiteAuthorityRuntimeState: ...


class SQLiteAuthorityRuntimeRereader:
    """Release-specific rereader backed by the restored authority and selectors."""

    def __init__(
        self,
        authority_path: Path,
        project_binding_path: Path,
        backend_selector_path: Path,
        runtime_selector_path: Path,
        *,
        active_release: str,
        previous_release: str,
    ) -> None:
        if not active_release or not previous_release:
            raise ControlStoreError("release-specific runtime selector identity is required")
        self._authority_path = authority_path
        self._project_binding_path = project_binding_path
        self._backend_selector_path = backend_selector_path
        self._runtime_selector_path = runtime_selector_path
        self._active_release = active_release
        self._previous_release = previous_release

    def reread_rollback(
        self, context: Mapping[str, object], result: Mapping[str, object]
    ) -> SQLiteAuthorityRuntimeState:
        project_id = context.get("project_id")
        fencing_token = context.get("fencing_token")
        if (
            context.get("backend") != "sqlite"
            or context.get("target") != "rollback"
            or not isinstance(project_id, str)
            or not isinstance(fencing_token, str)
            or set(result) != _RELEASE_EVIDENCE_FIELDS
            or any(
                result.get(field) is not True
                for field in (
                    "restored_verified",
                    "runtime_validated",
                    "backend_roundtrip_valid",
                )
            )
            or result.get("backend") != "sqlite"
            or result.get("fencing_token") != fencing_token
        ):
            raise ControlStoreError("rollback reread inputs are not release-specific")
        snapshot = inspect_sqlite_release_authority(
            self._authority_path,
            self._project_binding_path,
            self._backend_selector_path,
            self._runtime_selector_path,
            project_id,
            self._active_release,
            self._previous_release,
        )
        return SQLiteAuthorityRuntimeState(
            backend="sqlite",
            project_id=snapshot.project_id,
            authority_revision=snapshot.authority_revision,
            fencing_token=fencing_token,
            target="rollback",
            integrity_check=snapshot.integrity_check,
            foreign_key_violations=snapshot.foreign_key_violations,
            backend_roundtrip="sqlite",
        )


class SQLiteControlStoreAdapter:
    """Bind an engine adapter's rollback authority to a durable SQLite store."""

    def __init__(
        self,
        delegate: UpgradeAdapter,
        store: SQLiteRollbackControlStore,
        authority_runtime: AuthorityRuntimeRereader | None = None,
    ) -> None:
        if authority_runtime is None:
            raise ControlStoreError("concrete SQLite authority/runtime rereader is required")
        self._delegate = delegate
        self._store = store
        self._authority_runtime = authority_runtime
        self._release_authorization: _ReleaseAuthorization | None = None

    def snapshot(self, phase: str, context: Mapping[str, object]) -> Mapping[str, object]:
        return self._delegate.snapshot(phase, context)

    def execute(self, phase: str, context: Mapping[str, object]) -> Mapping[str, object]:
        return self._delegate.execute(phase, context)

    def verify_rollback_context(self, context: Mapping[str, object]) -> dict[str, object] | None:
        if self._store.operation_owned_by_current_thread:
            return self._store._verify_rollback_context_locked(context)
        return self._store.verify_rollback_context(context)

    def observe_backup_identity(
        self, backup: Path, manifest: Path, context: Mapping[str, object]
    ) -> BackupObservation:
        """Bind artifact evidence to a fresh durable control-store reread."""
        durable = self.verify_rollback_context(context)
        if not isinstance(durable, Mapping):
            raise ControlStoreError("backup observation requires a valid control-store reread")
        identity = durable.get("durable_barrier_id")
        revision = durable.get("revision")
        if not isinstance(identity, str) or type(revision) is not int:
            raise ControlStoreError("control-store reread identity is invalid")
        return BackupObservation.from_artifacts(
            backup,
            manifest,
            control_store_identity=identity,
            control_store_revision=revision,
        )

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
        try:
            reread = self._authority_runtime.reread_rollback(context, result)
        except Exception as error:
            raise ControlStoreError("authority/runtime rollback reread failed") from error
        if not isinstance(reread, SQLiteAuthorityRuntimeState) or (
            reread.backend != "sqlite"
            or reread.backend != context.get("backend")
            or reread.project_id != context.get("project_id")
            or reread.authority_revision != context.get("authority_revision")
            or reread.fencing_token != context.get("fencing_token")
            or reread.target != context.get("target")
            or reread.integrity_check != "ok"
            or type(reread.foreign_key_violations) is not int
            or reread.foreign_key_violations != 0
            or reread.backend_roundtrip != "sqlite"
        ):
            raise ControlStoreError("authority/runtime rollback reread is invalid")
        evidence = {
            "restored_verified": True,
            "runtime_validated": True,
            "backend_roundtrip_valid": True,
            "backend": reread.backend,
            "fencing_token": reread.fencing_token,
        }
        if durable["status"] == "releasing":
            if not self._store.operation_owned_by_current_thread:
                raise ControlStoreError("rollback revalidation requires the outer operation lock")
            self._release_authorization = self._store._authorize_release_locked(context, evidence)
        return dict(evidence)

    def operation_lock(self) -> AbstractContextManager[None]:
        return self._store.operation_lock()


def bind_control_store(
    backend: str,
    delegate: UpgradeAdapter,
    store: SQLiteRollbackControlStore | None,
    authority_runtime: AuthorityRuntimeRereader | None = None,
) -> SQLiteControlStoreAdapter:
    """Construct only a proven SQLite adapter; Git is explicitly fail-closed."""
    if backend != "sqlite" or store is None or store.authority_path is None:
        raise ControlStoreError(
            "durable rollback control store or authority binding is unavailable"
        )
    if authority_runtime is None:
        raise ControlStoreError("concrete SQLite authority/runtime rereader is required")
    return SQLiteControlStoreAdapter(delegate, store, authority_runtime)


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
            parent_status = os.fstat(descriptor)
            if parent_status.st_uid != os.geteuid() or stat.S_IMODE(parent_status.st_mode) != 0o700:
                os.close(descriptor)
                descriptor = -1
                raise ControlStoreError(
                    "control store requires an owner-only provisioned directory"
                )
        except OSError as error:
            if descriptor >= 0:
                os.close(descriptor)
            raise ControlStoreError("control store parent descriptor is unsafe") from error
        return descriptor, cls._file_identity(parent_status)

    @classmethod
    def _sidecar_identities(
        cls, parent: int, name: str, *, required: bool
    ) -> dict[str, tuple[int, int] | None]:
        identities: dict[str, tuple[int, int] | None] = {}
        for suffix in SIDECAR_SUFFIXES:
            descriptor = -1
            try:
                descriptor = os.open(name + suffix, os.O_RDWR | os.O_NOFOLLOW, dir_fd=parent)
                status = os.fstat(descriptor)
                if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                    raise ControlStoreError("control store sidecar is not private and regular")
                identities[suffix] = cls._file_identity(status)
            except FileNotFoundError:
                if required:
                    raise ControlStoreError("control store WAL sidecars are unavailable") from None
                identities[suffix] = None
            except OSError as error:
                raise ControlStoreError("control store sidecar descriptor is unsafe") from error
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        return identities

    @classmethod
    def _bind_sidecars(
        cls,
        parent: int,
        name: str,
        previous: Mapping[str, tuple[int, int] | None],
    ) -> dict[str, tuple[int, int] | None]:
        current = cls._sidecar_identities(parent, name, required=True)
        if any(previous[suffix] not in {None, current[suffix]} for suffix in SIDECAR_SUFFIXES):
            raise ControlStoreError("control store WAL sidecar identity changed")
        return current

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
    def _connection(self) -> Iterator[sqlite3.Connection]:  # noqa: C901
        self._recheck_authority()
        parent, descriptor = self._open_bound_file(
            self.path, self._parent_identity, self._control_identity
        )
        try:
            before_sidecars = self._sidecar_identities(parent, self.path.name, required=False)
        except Exception:
            os.close(descriptor)
            os.close(parent)
            raise
        bound_sidecars: dict[str, tuple[int, int] | None] | None = None
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
            bound_sidecars = self._bind_sidecars(parent, self.path.name, before_sidecars)
            yield connection
        except Exception:
            raise
        finally:
            try:
                try:
                    if bound_sidecars is not None:
                        current_sidecars = self._sidecar_identities(
                            parent, self.path.name, required=True
                        )
                        if current_sidecars != bound_sidecars:
                            raise ControlStoreError("control store WAL sidecar identity changed")
                finally:
                    connection.close()
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

    @contextmanager
    def lock_owned_by_caller(self, common_guard: CoordinatorLockGuard) -> Iterator[None]:
        """Hold the control lock under a caller-owned common-lock capability."""
        if not isinstance(common_guard, CoordinatorLockGuard):
            raise LockOwnershipError("caller-owned coordinator lock guard is required")
        common_guard.assert_owned()
        if common_guard.path != coordinator_lock_path().resolve():
            raise ControlStoreError("coordinator lock guard path mismatch")
        if self._operation_owner == threading.get_ident():
            raise ControlStoreError("control store lock is non-reentrant")
        with self._control_lock():
            if self._operation_owner is not None:
                raise ControlStoreError("control store operation is already active")
            self._operation_owner = threading.get_ident()
            try:
                common_guard.assert_owned()
                yield
                common_guard.assert_owned()
            finally:
                self._operation_owner = None

    @property
    def operation_owned_by_current_thread(self) -> bool:
        return self._operation_owner == threading.get_ident()

    @property
    def control_store_path(self) -> Path:
        """Return the canonical control-store path for identity contracts."""
        return self.path

    @property
    def control_lock_path(self) -> Path:
        """Return the canonical control-lock path for identity contracts."""
        return self._lock_path

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
            cursor = connection.execute(_INSERT_SQL, values)
            if cursor.rowcount != 1:
                connection.rollback()
                raise ControlStoreError("control barrier CAS insert lost its fence")
        else:
            cursor = connection.execute(
                _UPDATE_SQL,
                (
                    *(supplied[field] for field in columns if field != "operation_id"),
                    supplied["operation_id"],
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise ControlStoreError("control barrier CAS update lost its fence")
        try:
            connection.commit()
        except Exception as error:
            # A commit exception does not establish whether SQLite reached the
            # durable boundary.  Never turn that uncertainty into a success or
            # blindly retry a transition.  Fence the record into the terminal
            # ambiguous state using a fresh transaction instead.
            raise ControlStoreError(
                "control barrier commit outcome is ambiguous; durable state must be rechecked"
            ) from self._mark_ambiguous_after_commit_failure(
                connection, supplied, expected_revision, error
            )
        return dict(supplied)

    @staticmethod
    def _mark_ambiguous_after_commit_failure(
        connection: sqlite3.Connection,
        supplied: Mapping[str, object],
        expected_revision: int,
        commit_error: Exception,
    ) -> Exception:
        """Attempt to durably fence an uncertain CAS outcome as ambiguous."""
        try:
            connection.rollback()
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(_SELECT_SQL, (supplied["operation_id"],)).fetchone()
            if current is None:
                values = dict(supplied)
                values["status"] = "ambiguous"
                values["revision"] = expected_revision + 1
                connection.execute(
                    _INSERT_SQL,
                    tuple(values[field] for field in (*IDENTITY_FIELDS, "status", "revision")),
                )
            else:
                current_revision = int(current[-1])
                cursor = connection.execute(
                    "UPDATE barrier SET status='ambiguous',revision=? "
                    "WHERE operation_id=? AND revision=?",
                    (current_revision + 1, supplied["operation_id"], current_revision),
                )
                if cursor.rowcount != 1:
                    raise ControlStoreError("ambiguous barrier fencing lost its row fence")
            connection.commit()
        except Exception as recovery_error:
            raise ControlStoreError(
                "control barrier commit outcome is ambiguous and could not be durably fenced"
            ) from recovery_error
        return commit_error

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


class SQLiteBarrierSessionStore:
    """Durable CAS adapter for the target-neutral v10 barrier session.

    This is intentionally only the control-plane slice.  It persists the
    immutable session identity and its two child bindings, but it does not
    open or mutate the coordinator authority.  The enclosing
    ``SQLiteRollbackControlStore`` supplies the existing lock order (the
    repository lock followed by the control-store lock); authority fencing
    will be added by the later mutation adapter before upgrade execution is
    enabled.

    A missing row is the only ``absent`` state.  Once a row exists every
    transition is a compare-and-swap and identity fields are immutable.  A
    failed or uncertain observation must therefore be recorded as
    ``ambiguous``; there is no recovery shortcut that silently clears it.
    """

    _TABLE = "barrier_session"
    _SELECT = (
        "SELECT schema_version,project_id,attempt_id,state_revision,"
        "authority_revision_at_acquire,durable_barrier_id,fencing_token,fencing_owner,"
        "identity_digest,status,revision,forward_child,rollback_child "
        "FROM barrier_session WHERE project_id=?"
    )

    def __init__(
        self,
        control: SQLiteRollbackControlStore,
        authority_revision_reader: Callable[[], str] | None = None,
    ) -> None:
        self._control = control
        self.project_id = control.project_id
        self._authority_revision_reader = authority_revision_reader

    @property
    def operation_owned_by_current_thread(self) -> bool:
        return self._control.operation_owned_by_current_thread

    @property
    def control_store_path(self) -> Path:
        """Return the underlying control-store path for identity contracts."""
        return self._control.control_store_path

    @property
    def control_lock_path(self) -> Path:
        """Return the underlying control-lock path for identity contracts."""
        return self._control.control_lock_path

    @property
    def authority_path(self) -> Path | None:
        """Return the descriptor-bound authority path, when configured."""
        return self._control.authority_path

    def operation_lock(self) -> AbstractContextManager[None]:
        """Acquire the common lock, then this control store's lock."""
        return self._control.operation_lock()

    def lock_owned_by_caller(
        self, common_guard: CoordinatorLockGuard
    ) -> AbstractContextManager[None]:
        """Hold the control lock under a caller-owned common-lock capability."""
        return self._control.lock_owned_by_caller(common_guard)

    def snapshot_owned_by_caller(self) -> BarrierSessionState:
        """Read the durable session while the caller-owned control lock is held."""
        if not self.operation_owned_by_current_thread:
            raise ControlStoreError("caller-owned control lock is required")
        state = self._snapshot_locked()
        if state is None:
            raise ControlStoreError("durable barrier session is missing")
        return state

    def snapshot(self) -> BarrierSessionState | None:
        if self.operation_owned_by_current_thread:
            raise ControlStoreError("control store lock is non-reentrant")
        with self.operation_lock():
            return self._snapshot_locked()

    def _ensure_table(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS barrier_session (
                schema_version INTEGER NOT NULL,
                project_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL,
                state_revision INTEGER NOT NULL,
                authority_revision_at_acquire TEXT NOT NULL,
                durable_barrier_id TEXT NOT NULL,
                fencing_token TEXT NOT NULL,
                fencing_owner TEXT NOT NULL,
                identity_digest TEXT NOT NULL,
                status TEXT NOT NULL,
                revision INTEGER NOT NULL,
                forward_child TEXT,
                    rollback_child TEXT
                )"""
        )
        # Released sessions are immutable audit records.  The current-row
        # table remains one row per project for cheap admission checks, but
        # a subsequent attempt must not overwrite the released session.
        connection.execute(
            """CREATE TABLE IF NOT EXISTS barrier_session_history (
                    project_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    record_json TEXT NOT NULL,
                    PRIMARY KEY(project_id, attempt_id)
                )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS barrier_session_intent (
                    project_id TEXT NOT NULL,
                    intent_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL,
                    expected_revision INTEGER NOT NULL,
                    proposed_revision INTEGER NOT NULL,
                    proposed_status TEXT NOT NULL,
                    identity_digest TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    cause_code TEXT,
                    FOREIGN KEY(project_id) REFERENCES barrier_session(project_id)
                )"""
        )

    def _prepared_intents_locked(
        self, connection: sqlite3.Connection
    ) -> list[tuple[str, str, int, int, str, str]]:
        self._control._require_operation_lock()
        rows = connection.execute(
            "SELECT intent_id,attempt_id,expected_revision,proposed_revision,identity_digest,"
            "proposed_status "
            "FROM barrier_session_intent "
            "WHERE project_id=? AND outcome='prepared' ORDER BY proposed_revision",
            (self.project_id,),
        ).fetchall()
        return [
            (
                cast(str, row[0]),
                cast(str, row[1]),
                cast(int, row[2]),
                cast(int, row[3]),
                cast(str, row[4]),
                cast(str, row[5]),
            )
            for row in rows
        ]

    def _mark_intent_locked(
        self,
        connection: sqlite3.Connection,
        intent_id: str,
        outcome: str,
        cause_code: str | None = None,
    ) -> None:
        if outcome not in {"committed", "ambiguous", "reconciled"}:
            raise ControlStoreError("barrier session intent outcome is invalid")
        cursor = connection.execute(
            "UPDATE barrier_session_intent SET outcome=?,cause_code=? "
            "WHERE project_id=? AND intent_id=? AND outcome='prepared'",
            (outcome, cause_code, self.project_id, intent_id),
        )
        if cursor.rowcount != 1:
            raise ControlStoreError("barrier session intent outcome fence was lost")

    @staticmethod
    def _child_json(child: BarrierChildIdentity | None) -> str | None:
        if child is None:
            return None
        return json.dumps(
            {
                "operation_id": child.operation_id,
                "target": child.target,
                "barrier_identity_digest": child.barrier_identity_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    @staticmethod
    def _child(value: object) -> BarrierChildIdentity | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ControlStoreError("barrier child encoding is invalid")
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError) as error:
            raise ControlStoreError("barrier child encoding is invalid") from error
        if not isinstance(decoded, dict) or set(decoded) != {
            "operation_id",
            "target",
            "barrier_identity_digest",
        }:
            raise ControlStoreError("barrier child encoding is invalid")
        if not all(isinstance(item, str) for item in decoded.values()):
            raise ControlStoreError("barrier child encoding is invalid")
        return BarrierChildIdentity(
            operation_id=decoded["operation_id"],
            target=decoded["target"],
            barrier_identity_digest=decoded["barrier_identity_digest"],
        )

    def _row_state(self, row: tuple[object, ...]) -> BarrierSessionState:
        if len(row) != 13:
            raise ControlStoreError("barrier session row is invalid")
        identity_record = dict(
            zip(
                (
                    "schema_version",
                    "project_id",
                    "attempt_id",
                    "state_revision",
                    "authority_revision_at_acquire",
                    "durable_barrier_id",
                    "fencing_token",
                    "fencing_owner",
                    "identity_digest",
                ),
                row[:9],
                strict=True,
            )
        )
        try:
            identity = BarrierSessionIdentity.from_record(identity_record)
        except (UpgradeIdentityError, TypeError) as error:
            raise ControlStoreError("barrier session identity is invalid") from error
        try:
            state = BarrierSessionState(
                identity,
                cast(str, row[9]),
                cast(int, row[10]),
                self._child(row[11]),
                self._child(row[12]),
            )
        except (ControlStoreError, TypeError, ValueError) as error:
            raise ControlStoreError("barrier session state is invalid") from error
        if identity.project_id != self.project_id:
            raise ControlStoreError("barrier session project binding mismatch")
        return state

    def _snapshot_locked(self) -> BarrierSessionState | None:
        self._control._require_operation_lock()
        with self._control._connection() as connection:
            self._ensure_table(connection)
            row = connection.execute(self._SELECT, (self.project_id,)).fetchone()
            return None if row is None else self._row_state(row)

    def create(self, identity: BarrierSessionIdentity) -> BarrierSessionState:
        """Persist the initial held state; a second attempt is rejected."""
        return self.cas(0, BarrierSessionState(identity, "held", 1))

    def recheck_held(  # noqa: C901
        self,
        expected_revision: int | BarrierSessionState,
        fresh_authority_revision: str | None = None,
    ) -> BarrierSessionState:
        """Durably reread a held session before each fenced operation.

        This is intentionally read-only: the project fence and session
        revision are not advanced by a recheck.  A caller may additionally
        provide the freshly observed authority revision; a mismatch rejects
        admission instead of treating stale evidence as a held barrier.
        """
        expected_identity: BarrierSessionIdentity | None = None
        if isinstance(expected_revision, BarrierSessionState):
            expected_identity = expected_revision.identity
            expected_revision = expected_revision.revision
        if type(expected_revision) is not int or expected_revision < 1:
            raise ControlStoreError("barrier session expected revision is invalid")
        if fresh_authority_revision is not None:
            raise ControlStoreError("fresh authority revision must come from the trusted rereader")
        if self._authority_revision_reader is None:
            raise ControlStoreError("fresh authority rereader is required")
        try:
            fresh_authority_revision = self._authority_revision_reader()
        except Exception as error:
            raise ControlStoreError("fresh authority reread failed") from error
        if not isinstance(fresh_authority_revision, str) or not fresh_authority_revision:
            raise ControlStoreError("fresh authority revision is invalid")
        if self.operation_owned_by_current_thread:
            raise ControlStoreError("control store lock is non-reentrant")
        with self.operation_lock():
            current = self._snapshot_locked()
            if current is None:
                raise ControlStoreError("barrier session is absent")
            if current.revision != expected_revision:
                raise ControlStoreError("barrier session CAS conflict")
            if expected_identity is not None and expected_identity != current.identity:
                raise ControlStoreError("barrier session identity changed")
            if current.status != "held":
                raise ControlStoreError("barrier session is not held")
            if (
                fresh_authority_revision is not None
                and current.identity.authority_revision_at_acquire != fresh_authority_revision
            ):
                raise ControlStoreError("barrier session authority revision changed")
            return current

    def recheck_held_locked(  # noqa: C901
        self,
        common_guard: CoordinatorLockGuard,
        expected_identity: BarrierSessionIdentity,
        expected_revision: int,
    ) -> BarrierSessionState:
        """Read-only held-session recheck while the caller owns both locks."""
        if not isinstance(common_guard, CoordinatorLockGuard):
            raise LockOwnershipError("caller-owned coordinator lock guard is required")
        common_guard.assert_owned()
        if common_guard.path != coordinator_lock_path().resolve():
            raise ControlStoreError("coordinator lock guard path mismatch")
        if not isinstance(expected_identity, BarrierSessionIdentity):
            raise ControlStoreError("barrier session identity is required")
        if type(expected_revision) is not int or expected_revision < 1:
            raise ControlStoreError("barrier session expected revision is invalid")
        self._control._require_operation_lock()
        if self._authority_revision_reader is None:
            raise ControlStoreError("fresh authority rereader is required")
        try:
            fresh_authority_revision = self._authority_revision_reader()
        except Exception as error:
            raise ControlStoreError("fresh authority reread failed") from error
        if not isinstance(fresh_authority_revision, str) or not fresh_authority_revision:
            raise ControlStoreError("fresh authority revision is invalid")
        current = self._snapshot_locked()
        if current is None:
            raise ControlStoreError("barrier session is absent")
        if current.revision != expected_revision:
            raise ControlStoreError("barrier session CAS conflict")
        if current.identity != expected_identity:
            raise ControlStoreError("barrier session identity changed")
        if current.status != "held":
            raise ControlStoreError("barrier session is not held")
        if current.identity.authority_revision_at_acquire != fresh_authority_revision:
            raise ControlStoreError("barrier session authority revision changed")
        common_guard.assert_owned()
        return current

    def cas(self, expected_revision: int, state: BarrierSessionState) -> BarrierSessionState:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ControlStoreError("barrier session expected revision is invalid")
        if state.identity.project_id != self.project_id:
            raise ControlStoreError("barrier session project binding mismatch")
        if state.status == "released" and expected_revision == 0:
            raise ControlStoreError("new barrier session must start held")
        if self.operation_owned_by_current_thread:
            raise ControlStoreError("control store lock is non-reentrant")
        with self.operation_lock():
            return self._cas_locked(expected_revision, state)

    def _cas_locked(  # noqa: C901
        self, expected_revision: int, supplied: BarrierSessionState
    ) -> BarrierSessionState:
        self._control._require_operation_lock()
        with self._control._connection() as connection:
            self._ensure_table(connection)
            connection.execute("BEGIN IMMEDIATE")
            current_row = connection.execute(self._SELECT, (self.project_id,)).fetchone()
            replacing_released = False
            if current_row is None:
                if expected_revision != 0:
                    connection.rollback()
                    raise ControlStoreError("barrier session does not exist")
                next_revision = 1
                if supplied.status != "held":
                    connection.rollback()
                    raise ControlStoreError("new barrier session must start held")
            else:
                current = self._row_state(current_row)
                # A released session is terminal, but a new attempt may be
                # admitted with CAS(0).  Preserve the old terminal record in
                # history before replacing the current-session pointer.
                replacing_released = (
                    expected_revision == 0
                    and current.status == "released"
                    and supplied.status == "held"
                    and supplied.identity != current.identity
                )
                if replacing_released and (
                    supplied.identity.attempt_id == current.identity.attempt_id
                    or supplied.identity.state_revision <= current.identity.state_revision
                ):
                    connection.rollback()
                    raise ControlStoreError(
                        "fresh barrier session requires a distinct newer project fence"
                    )
                if not replacing_released and current.revision != expected_revision:
                    connection.rollback()
                    raise ControlStoreError("barrier session CAS conflict")
                if not replacing_released and supplied.identity != current.identity:
                    connection.rollback()
                    raise ControlStoreError("barrier session identity changed")
                if (
                    not replacing_released
                    and supplied.status not in STATUS_TRANSITIONS[current.status]
                ):
                    connection.rollback()
                    raise ControlStoreError("illegal barrier session transition")
                if not replacing_released and supplied.revision != expected_revision + 1:
                    connection.rollback()
                    raise ControlStoreError("barrier session revision is not monotonic")
                next_revision = 1 if replacing_released else supplied.revision
                if replacing_released:
                    connection.execute(
                        "INSERT OR REPLACE INTO barrier_session_history "
                        "(project_id,attempt_id,revision,record_json) VALUES (?,?,?,?)",
                        (
                            self.project_id,
                            current.identity.attempt_id,
                            current.revision,
                            json.dumps(
                                {
                                    **current.identity.as_record(),
                                    "status": current.status,
                                    "revision": current.revision,
                                    "forward_child": self._child_json(current.forward_child),
                                    "rollback_child": self._child_json(current.rollback_child),
                                },
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        ),
                    )
                    connection.execute(
                        "DELETE FROM barrier_session WHERE project_id=?", (self.project_id,)
                    )
            if supplied.revision != next_revision:
                connection.rollback()
                raise ControlStoreError("barrier session revision is invalid")
            intent_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO barrier_session_intent "
                "(project_id,intent_id,attempt_id,expected_revision,proposed_revision,"
                "proposed_status,identity_digest,outcome,cause_code) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    self.project_id,
                    intent_id,
                    supplied.identity.attempt_id,
                    expected_revision,
                    supplied.revision,
                    supplied.status,
                    supplied.identity.identity_digest,
                    "prepared",
                    None,
                ),
            )
            values = (
                *supplied.identity.as_record().values(),
                supplied.status,
                supplied.revision,
                self._child_json(supplied.forward_child),
                self._child_json(supplied.rollback_child),
            )
            if current_row is None:
                cursor = connection.execute(
                    "INSERT INTO barrier_session "
                    "(schema_version,project_id,attempt_id,state_revision,"
                    "authority_revision_at_acquire,durable_barrier_id,fencing_token,"
                    "fencing_owner,identity_digest,status,revision,forward_child,rollback_child) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
            else:
                if replacing_released:
                    cursor = connection.execute(
                        "INSERT INTO barrier_session "
                        "(schema_version,project_id,attempt_id,state_revision,"
                        "authority_revision_at_acquire,durable_barrier_id,fencing_token,"
                        "fencing_owner,identity_digest,status,revision,forward_child,"
                        "rollback_child) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        values,
                    )
                else:
                    cursor = connection.execute(
                        "UPDATE barrier_session SET status=?,revision=?,forward_child=?,"
                        "rollback_child=? WHERE project_id=? AND revision=?",
                        (
                            supplied.status,
                            supplied.revision,
                            values[-2],
                            values[-1],
                            self.project_id,
                            expected_revision,
                        ),
                    )
            if cursor.rowcount != 1:
                connection.rollback()
                raise ControlStoreError("barrier session CAS update lost its fence")
            try:
                connection.commit()
            except Exception as error:
                raise ControlStoreError(
                    "barrier session commit outcome is ambiguous; durable state must be rechecked"
                ) from self._mark_ambiguous_after_commit_failure(
                    connection, supplied, expected_revision, error
                )
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._mark_intent_locked(connection, intent_id, "committed")
                connection.commit()
            except Exception as error:
                raise ControlStoreError(
                    "barrier session outcome publication is ambiguous; recovery is required"
                ) from error
            return supplied

    def recover_unknown(self) -> BarrierSessionState | None:
        """Fence every prepared outcome left by a process death or lost reply."""
        if self.operation_owned_by_current_thread:
            raise ControlStoreError("control store lock is non-reentrant")
        with self.operation_lock(), self._control._connection() as connection:
            self._ensure_table(connection)
            prepared = self._prepared_intents_locked(connection)
            if not prepared:
                return self._snapshot_locked()
            current = self._snapshot_locked()
            if current is None:
                raise ControlStoreError("prepared session intent has no session")
            for (
                _intent_id,
                attempt_id,
                expected_revision,
                proposed_revision,
                identity_digest,
                proposed_status,
            ) in prepared:
                if (
                    attempt_id != current.identity.attempt_id
                    or identity_digest != current.identity.identity_digest
                    or proposed_revision != current.revision
                    or expected_revision != current.revision - 1
                    or proposed_status not in STATUS_TRANSITIONS
                    or proposed_status != current.status
                ):
                    raise ControlStoreError("prepared session intent identity is invalid")
            if current.status != "ambiguous":
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE barrier_session SET status='ambiguous',revision=? "
                    "WHERE project_id=? AND revision=? AND status IN ('held','releasing')",
                    (current.revision + 1, self.project_id, current.revision),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    connection.rollback()
                    raise ControlStoreError("unknown session outcome lost its row fence")
                current = BarrierSessionState(
                    current.identity,
                    "ambiguous",
                    current.revision + 1,
                    current.forward_child,
                    current.rollback_child,
                )
                connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            for intent_id, _attempt_id, _expected, _proposed, _digest, _status in prepared:
                self._mark_intent_locked(connection, intent_id, "ambiguous", "process-death")
            connection.commit()
            return current

    def reconcile_ambiguous(  # noqa: C901
        self,
        expected_revision: int,
        replacement: BarrierSessionState,
    ) -> BarrierSessionState:
        """Replace an ambiguous session only with a distinct newer fence."""
        if type(expected_revision) is not int or expected_revision < 1:
            raise ControlStoreError("barrier session expected revision is invalid")
        if (
            replacement.status != "held"
            or replacement.revision != 1
            or replacement.identity.project_id != self.project_id
        ):
            raise ControlStoreError("ambiguous reconciliation requires a new held session")
        if self.operation_owned_by_current_thread:
            raise ControlStoreError("control store lock is non-reentrant")
        with self.operation_lock(), self._control._connection() as connection:
            self._ensure_table(connection)
            current = self._snapshot_locked()
            if current is None or current.status != "ambiguous":
                raise ControlStoreError("only ambiguous sessions require reconciliation")
            if current.revision != expected_revision:
                raise ControlStoreError("barrier session CAS conflict")
            if (
                replacement.identity.attempt_id == current.identity.attempt_id
                or replacement.identity.state_revision <= current.identity.state_revision
                or replacement.identity.durable_barrier_id == current.identity.durable_barrier_id
                or replacement.identity.fencing_token == current.identity.fencing_token
            ):
                raise ControlStoreError("ambiguous reconciliation requires a distinct newer fence")
            if self._prepared_intents_locked(connection):
                raise ControlStoreError("ambiguous reconciliation has unresolved intent")
            if self._authority_revision_reader is None:
                raise ControlStoreError("fresh authority rereader is required")
            try:
                fresh_authority_revision = self._authority_revision_reader()
            except Exception as error:
                raise ControlStoreError("fresh authority reread failed") from error
            if not isinstance(fresh_authority_revision, str) or not fresh_authority_revision:
                raise ControlStoreError("fresh authority revision is invalid")
            if fresh_authority_revision != replacement.identity.authority_revision_at_acquire:
                raise ControlStoreError("replacement authority revision changed")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR REPLACE INTO barrier_session_history "
                "(project_id,attempt_id,revision,record_json) VALUES (?,?,?,?)",
                (
                    self.project_id,
                    current.identity.attempt_id,
                    current.revision,
                    json.dumps(
                        {
                            **current.identity.as_record(),
                            "status": current.status,
                            "revision": current.revision,
                            "forward_child": self._child_json(current.forward_child),
                            "rollback_child": self._child_json(current.rollback_child),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
            connection.execute("DELETE FROM barrier_session WHERE project_id=?", (self.project_id,))
            values = (
                *replacement.identity.as_record().values(),
                replacement.status,
                replacement.revision,
                self._child_json(replacement.forward_child),
                self._child_json(replacement.rollback_child),
            )
            connection.execute(
                "INSERT INTO barrier_session "
                "(schema_version,project_id,attempt_id,state_revision,authority_revision_at_acquire,"
                "durable_barrier_id,fencing_token,fencing_owner,identity_digest,status,revision,"
                "forward_child,rollback_child) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            connection.commit()
            return replacement

    def _mark_ambiguous_after_commit_failure(
        self,
        connection: sqlite3.Connection,
        supplied: BarrierSessionState,
        expected_revision: int,
        commit_error: Exception,
    ) -> Exception:
        """Fence an uncertain session CAS outcome into durable ambiguity."""
        try:
            connection.rollback()
            connection.execute("BEGIN IMMEDIATE")
            current_row = connection.execute(self._SELECT, (self.project_id,)).fetchone()
            if current_row is None:
                ambiguous = BarrierSessionState(
                    supplied.identity,
                    "ambiguous",
                    expected_revision + 1,
                    supplied.forward_child,
                    supplied.rollback_child,
                )
                values = (
                    *ambiguous.identity.as_record().values(),
                    ambiguous.status,
                    ambiguous.revision,
                    self._child_json(ambiguous.forward_child),
                    self._child_json(ambiguous.rollback_child),
                )
                cursor = connection.execute(
                    "INSERT INTO barrier_session "
                    "(schema_version,project_id,attempt_id,state_revision,"
                    "authority_revision_at_acquire,durable_barrier_id,fencing_token,"
                    "fencing_owner,identity_digest,status,revision,forward_child,rollback_child) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
            else:
                current = self._row_state(current_row)
                cursor = connection.execute(
                    "UPDATE barrier_session SET status='ambiguous',revision=? "
                    "WHERE project_id=? AND revision=?",
                    (current.revision + 1, self.project_id, current.revision),
                )
            if cursor.rowcount != 1:
                raise ControlStoreError("ambiguous session fencing lost its row fence")
            connection.commit()
        except Exception as recovery_error:
            raise ControlStoreError(
                "barrier session commit outcome is ambiguous and could not be durably fenced"
            ) from recovery_error
        return commit_error

    def bind_child(
        self, expected_revision: int, child: BarrierChildIdentity
    ) -> BarrierSessionState:
        current = self.snapshot()
        if current is None:
            raise ControlStoreError("barrier session is absent")
        if expected_revision != current.revision:
            raise ControlStoreError("barrier session revision conflict")
        contract = BarrierSessionContract(current.identity)
        # Reconstruct only to reuse the already-tested transition rules.
        contract._state = current
        return self.cas(expected_revision, contract.bind_child(expected_revision, child))

    def begin_reopen(self, expected_revision: int, target: str) -> BarrierSessionState:
        current = self.snapshot()
        if current is None:
            raise ControlStoreError("barrier session is absent")
        contract = BarrierSessionContract(current.identity)
        contract._state = current
        return self.cas(expected_revision, contract.begin_reopen(expected_revision, target))

    def complete_reopen(
        self, expected_revision: int, runtime_verified: bool
    ) -> BarrierSessionState:
        current = self.snapshot()
        if current is None:
            raise ControlStoreError("barrier session is absent")
        contract = BarrierSessionContract(current.identity)
        contract._state = current
        return self.cas(
            expected_revision,
            contract.complete_reopen(expected_revision, runtime_verified),
        )

    def mark_ambiguous(self, expected_revision: int, cause_code: str) -> BarrierSessionState:
        current = self.snapshot()
        if current is None:
            raise ControlStoreError("barrier session is absent")
        contract = BarrierSessionContract(current.identity)
        contract._state = current
        return self.cas(expected_revision, contract.mark_ambiguous(expected_revision, cause_code))
