# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Read-only SQLite authority evidence for the future scoped adapter."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar, cast

from tools.admission_lease import AdmissionLease, AdmissionRecheck
from tools.lifecycle_session import LifecycleSession, _issue
from tools.lock_domain_scope import LockDomainScope
from tools.rollback_control_store import BarrierSessionState, SQLiteBarrierSessionStore
from tools.rollback_evidence import BackupObservation
from tools.upgrade_engine import JournalSnapshot


class SQLiteAuthorityError(RuntimeError):
    """SQLite authority evidence is unavailable or mutation was requested."""


_EffectResult = TypeVar("_EffectResult")


@dataclass(frozen=True, slots=True)
class SQLiteLifecycleSnapshot:
    """Read-only paired durable control/session and journal snapshot."""

    control: BarrierSessionState
    journal: JournalSnapshot
    journal_identity: tuple[int, int, int, int]
    control_lock_identity: tuple[int, int, int, int]
    control_store_identity: tuple[int, int, int, int]


_SUPPORTED_PHASES = frozenset(
    {
        "discover",
        "preflight",
        "quiesce",
        "backup",
        "stage",
        "commit",
        "validate",
        "reopen",
        "rollback",
    }
)


class SQLiteLifecycleExecutor:
    """Concrete adapter-owned backup/restore executor; no phase authorization."""

    def __init__(
        self,
        adapter: SQLiteAuthorityAdapter,
        session_store: SQLiteBarrierSessionStore | None = None,
        journal: Path | None = None,
    ) -> None:
        if session_store is not None and session_store.authority_path != adapter._authority:
            raise SQLiteAuthorityError("lifecycle session store is bound to a foreign authority")
        if session_store is not None and journal is None:
            raise SQLiteAuthorityError("lifecycle executor journal is required")
        self._adapter = adapter
        self._session_store = session_store
        self._journal = journal
        self._last_snapshot: SQLiteLifecycleSnapshot | None = None

    @classmethod
    def bind(
        cls,
        adapter: SQLiteAuthorityAdapter,
        session_store: SQLiteBarrierSessionStore,
        journal: Path,
    ) -> SQLiteLifecycleExecutor:
        """Bind the executor to concrete durable control and journal sources."""
        if session_store.authority_path != adapter._authority:
            raise SQLiteAuthorityError("lifecycle session store is bound to a foreign authority")
        return cls(adapter, session_store, journal.absolute())

    def snapshot(self) -> SQLiteLifecycleSnapshot:
        """Capture both durable sources under the control-store operation lock."""
        if self._session_store is None or self._journal is None:
            raise SQLiteAuthorityError("lifecycle executor is not bound to durable state")
        try:
            with self._session_store.operation_lock():
                return self._snapshot_locked()
        except SQLiteAuthorityError:
            raise
        except Exception as error:
            raise SQLiteAuthorityError("durable lifecycle snapshot failed") from error

    def _snapshot_locked(self) -> SQLiteLifecycleSnapshot:
        """Capture durable state while the caller already owns the operation lock."""
        if self._session_store is None or self._journal is None:
            raise SQLiteAuthorityError("lifecycle executor is not bound to durable state")
        control_store_identity = self._control_store_identity(
            self._session_store.control_store_path
        )
        self._adapter._check_identity()
        control = self._session_store.snapshot_owned_by_caller()
        if (
            self._control_store_identity(self._session_store.control_store_path)
            != control_store_identity
        ):
            raise SQLiteAuthorityError("lifecycle control store identity changed")
        journal_identity = self._journal_identity(self._journal)
        value = json.loads(self._journal.read_text(encoding="utf-8"))
        journal = JournalSnapshot.from_mapping(cast(Mapping[str, object], value))
        if self._journal_identity(self._journal) != journal_identity:
            raise SQLiteAuthorityError("lifecycle journal identity changed")
        self._adapter._check_identity()
        lock_identity = self._lock_identity(self._session_store.control_lock_path)
        snapshot = SQLiteLifecycleSnapshot(
            control, journal, journal_identity, lock_identity, control_store_identity
        )
        self._last_snapshot = snapshot
        return snapshot

    def _run_effect(self, operation: Callable[[], _EffectResult]) -> _EffectResult:
        """Run one bound operation with locked before/after durable rereads."""
        if self._session_store is None or self._journal is None:
            raise SQLiteAuthorityError("lifecycle executor is not bound to durable state")
        try:
            with self._session_store.operation_lock():
                before = self._snapshot_locked()
                try:
                    result = operation()
                except Exception as error:
                    try:
                        self._assert_snapshot_locked(before)
                    except SQLiteAuthorityError as state_error:
                        raise state_error from error
                    raise
                self._assert_snapshot_locked(before)
                return result
        except SQLiteAuthorityError:
            raise
        except Exception as error:
            raise SQLiteAuthorityError("lifecycle effect failed") from error

    def _assert_snapshot_locked(self, expected: SQLiteLifecycleSnapshot) -> None:
        current = self._snapshot_locked()
        if current != expected:
            raise SQLiteAuthorityError("durable lifecycle state changed")

    @staticmethod
    def _journal_identity(path: Path) -> tuple[int, int, int, int]:
        try:
            parent = path.parent.lstat()
            value = path.lstat()
        except OSError as error:
            raise SQLiteAuthorityError("lifecycle journal is unavailable") from error
        if not stat.S_ISDIR(parent.st_mode):
            raise SQLiteAuthorityError("lifecycle journal parent is not a directory")
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            raise SQLiteAuthorityError("lifecycle journal is not private and regular")
        return parent.st_dev, parent.st_ino, value.st_dev, value.st_ino

    @staticmethod
    def _lock_identity(path: Path) -> tuple[int, int, int, int]:
        try:
            parent = path.parent.lstat()
            value = path.lstat()
        except OSError as error:
            raise SQLiteAuthorityError("lifecycle control lock is unavailable") from error
        if not stat.S_ISDIR(parent.st_mode):
            raise SQLiteAuthorityError("lifecycle control lock parent is not a directory")
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_nlink != 1
            or value.st_uid != os.geteuid()
            or stat.S_IMODE(value.st_mode) != 0o600
        ):
            raise SQLiteAuthorityError("lifecycle control lock is not private and regular")
        return parent.st_dev, parent.st_ino, value.st_dev, value.st_ino

    @staticmethod
    def _control_store_identity(path: Path) -> tuple[int, int, int, int]:
        try:
            parent = path.parent.lstat()
            value = path.lstat()
        except OSError as error:
            raise SQLiteAuthorityError("lifecycle control store is unavailable") from error
        if not stat.S_ISDIR(parent.st_mode):
            raise SQLiteAuthorityError("lifecycle control store parent is not a directory")
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            raise SQLiteAuthorityError("lifecycle control store is not private and regular")
        return parent.st_dev, parent.st_ino, value.st_dev, value.st_ino

    def assert_snapshot_stable(self, expected: SQLiteLifecycleSnapshot) -> SQLiteLifecycleSnapshot:
        """Reread durable state and fail closed if it changed since ``expected``."""
        if expected is not self._last_snapshot:
            raise SQLiteAuthorityError("lifecycle snapshot belongs to a foreign executor")
        current = self.snapshot()
        if current != expected:
            raise SQLiteAuthorityError("durable lifecycle state changed")
        return current

    def backup(self, destination: Path, binding: dict[str, Any]) -> dict[str, Any]:
        return self._run_effect(lambda: self._adapter.backup_bound(destination, binding))

    def restore(
        self, backup: Path, destination: Path, manifest: dict[str, Any], binding: dict[str, Any]
    ) -> None:
        self._run_effect(
            lambda: self._adapter.restore_bound(backup, destination, manifest, binding)
        )


class SQLiteAuthorityAdapter:
    """Read-only integrity evidence adapter; execute remains disabled."""

    requires_bound_rollback = True
    bound_rollback_kind = "sqlite"

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
        self._session_identity = (descriptor.st_dev, descriptor.st_ino)
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

    def lifecycle_session(self) -> LifecycleSession:
        """Return an opaque session bound to this adapter's authority."""
        return _issue(self, self._authority)

    def lifecycle_executor(self) -> SQLiteLifecycleExecutor:
        return SQLiteLifecycleExecutor(self)

    def bind_lifecycle_executor(
        self, session_store: SQLiteBarrierSessionStore, journal: Path
    ) -> SQLiteLifecycleExecutor:
        """Return an executor bound to this adapter's durable state sources."""
        return SQLiteLifecycleExecutor.bind(self, session_store, journal)

    def backup_bound(self, destination: Path, binding: dict[str, Any]) -> dict[str, Any]:
        from tools.sqlite_backup import backup_database

        return backup_database(
            self._authority, destination, binding, session=self.lifecycle_session(), owner=self
        )

    def restore_bound(
        self,
        backup: Path,
        destination: Path,
        manifest: dict[str, Any],
        binding: dict[str, Any],
    ) -> None:
        from tools.sqlite_backup import restore_database

        restore_database(
            backup,
            destination,
            manifest,
            binding,
            quiesced=True,
            session=_issue(self, backup),
            owner=self,
        )

    @staticmethod
    def observe_backup_identity(
        backup: Path,
        manifest: Path,
        *,
        control_store_identity: str,
        control_store_revision: int,
    ) -> BackupObservation:
        """Read backup artifacts and CAS identity without authorizing restore."""
        return BackupObservation.from_artifacts(
            backup,
            manifest,
            control_store_identity=control_store_identity,
            control_store_revision=control_store_revision,
        )

    @staticmethod
    def verify_backup_artifact(
        backup: Path, manifest: Mapping[str, object], binding: Mapping[str, object]
    ) -> dict[str, object]:
        """Run SQLite manifest, digest, integrity, and FK checks read-only."""
        from tools.sqlite_backup import verify_backup

        return verify_backup(backup, dict(manifest), dict(binding))

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
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
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
        schema_version = context.get("schema_version")
        state_revision = context.get("state_revision")
        if type(schema_version) is not int or schema_version < 1:
            raise SQLiteAuthorityError("SQLite authority context types are invalid")
        if type(state_revision) is not int or state_revision < 1:
            raise SQLiteAuthorityError("SQLite authority context types are invalid")
        for field in required - {"schema_version", "state_revision"}:
            value = context.get(field)
            if type(value) is not str or not value:
                raise SQLiteAuthorityError("SQLite authority context types are invalid")
        if context.get("target") not in {"new", "rollback"}:
            raise SQLiteAuthorityError("SQLite authority context target is invalid")
        return dict(context)

    def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, Any]:
        """Return read-only integrity evidence without claiming mutation safety."""
        if type(phase) is not str or phase not in _SUPPORTED_PHASES:
            raise SQLiteAuthorityError("SQLite authority phase is invalid")
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

    def snapshot_bound(  # noqa: C901
        self,
        phase: str,
        context: Mapping[str, object],
        scope: LockDomainScope,
        *,
        lease: AdmissionLease,
        admission_recheck: AdmissionRecheck,
    ) -> dict[str, Any]:
        """Read SQLite integrity only inside a trusted, identity-bound scope."""
        from tools.scoped_backend_adapter import ScopedBackendAdapter

        if type(phase) is not str or phase not in _SUPPORTED_PHASES:
            raise SQLiteAuthorityError("SQLite authority phase is invalid")
        if not isinstance(lease, AdmissionLease):
            raise SQLiteAuthorityError("trusted admission lease is required")
        if not isinstance(admission_recheck, AdmissionRecheck):
            raise SQLiteAuthorityError("trusted admission recheck is required")
        if admission_recheck.lease != lease:
            raise SQLiteAuthorityError("trusted admission recheck does not match lease")
        try:
            validated_context = self._context(context)
        except SQLiteAuthorityError:
            raise
        except Exception as error:
            raise SQLiteAuthorityError("SQLite authority context is invalid") from error
        if not isinstance(scope, LockDomainScope):
            raise SQLiteAuthorityError("concrete lock-domain scope is required")
        expected_identity = {
            "project_id": lease.project_id,
            "authority_revision": lease.authority_revision,
            "fencing_token": lease.fencing_token,
            "fencing_owner": lease.fencing_owner,
            "durable_barrier_id": lease.durable_barrier_id,
            "state_revision": lease.revision,
        }
        if any(validated_context.get(name) != value for name, value in expected_identity.items()):
            raise SQLiteAuthorityError("trusted session identity changed")
        try:
            value = ScopedBackendAdapter(self, scope).snapshot(
                phase, validated_context, scope_context=expected_identity
            )
        except SQLiteAuthorityError:
            raise
        except (TypeError, RuntimeError) as error:
            raise SQLiteAuthorityError("trusted SQLite session reread was rejected") from error
        expected_result_keys = set(validated_context) | {
            "phase",
            "backend_identity_verified",
            "sqlite_integrity_verified",
            "sqlite_foreign_keys_verified",
            "mutates_authority",
        }
        if set(value) != expected_result_keys:
            raise SQLiteAuthorityError("SQLite authority backend result schema changed")
        for field, expected in validated_context.items():
            if value.get(field) != expected or type(value.get(field)) is not type(expected):
                raise SQLiteAuthorityError("SQLite authority backend context identity changed")
        if type(value.get("phase")) is not str or value.get("phase") != phase:
            raise SQLiteAuthorityError("SQLite authority backend phase changed")
        if (
            type(value.get("backend_identity_verified")) is not bool
            or value.get("backend_identity_verified") is not True
        ):
            raise SQLiteAuthorityError("SQLite authority backend identity is unverified")
        if (
            type(value.get("sqlite_integrity_verified")) is not bool
            or value.get("sqlite_integrity_verified") is not True
        ):
            raise SQLiteAuthorityError("SQLite authority integrity is unverified")
        if (
            type(value.get("sqlite_foreign_keys_verified")) is not bool
            or value.get("sqlite_foreign_keys_verified") is not True
        ):
            raise SQLiteAuthorityError("SQLite authority foreign keys are unverified")
        if (
            type(value.get("mutates_authority")) is not bool
            or value.get("mutates_authority") is not False
        ):
            raise SQLiteAuthorityError("SQLite authority backend is not read-only")
        return value

    def verify_rollback_context(self, context: Mapping[str, object]) -> dict[str, Any]:
        if context.get("target") != "rollback":
            raise SQLiteAuthorityError("SQLite rollback context target is invalid")
        value = self.snapshot("rollback", context)
        value["rollback_context_verified"] = False
        return value

    def verify_rollback_context_bound(
        self,
        context: Mapping[str, object],
        scope: LockDomainScope,
        *,
        lease: AdmissionLease,
        admission_recheck: AdmissionRecheck,
    ) -> dict[str, Any]:
        """Reread rollback evidence only inside a trusted scope."""
        if context.get("target") != "rollback":
            raise SQLiteAuthorityError("SQLite rollback context target is invalid")
        value = self.snapshot_bound(
            "rollback",
            context,
            scope,
            lease=lease,
            admission_recheck=admission_recheck,
        )
        value["rollback_context_verified"] = False
        return value

    def execute(self, _phase: str, _context: Mapping[str, object]) -> dict[str, Any]:
        raise SQLiteAuthorityError("SQLite authority mutation adapter is not implemented")
