# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Read-only SQLite authority evidence for the future scoped adapter."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
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

    def assert_selector_binding(
        self,
        selector_ref: str,
        expected_state_revision: int,
        barrier_id: str,
        fencing_token: str,
    ) -> SQLiteLifecycleSnapshot:
        """Return a stable held-session snapshot for selector publication.

        This is deliberately a read-only admission seam: selector mutation is
        still disabled until an adapter can perform the publication atomically.
        """
        snapshot = self.snapshot()
        self._check_selector_binding(
            snapshot, selector_ref, expected_state_revision, barrier_id, fencing_token
        )
        return snapshot

    @staticmethod
    def _check_selector_binding(
        snapshot: SQLiteLifecycleSnapshot,
        selector_ref: str,
        expected_state_revision: int,
        barrier_id: str,
        fencing_token: str,
    ) -> None:
        identity = snapshot.control.identity
        if (
            snapshot.control.status != "held"
            or identity.state_revision != expected_state_revision
            or identity.durable_barrier_id != barrier_id
            or identity.fencing_token != fencing_token
            or not selector_ref
        ):
            raise SQLiteAuthorityError("selector publication binding is invalid")

    @contextmanager
    def selector_visibility_scope(
        self,
        selector_ref: str,
        expected_state_revision: int,
        barrier_id: str,
        fencing_token: str,
    ) -> Iterator[SQLiteLifecycleSnapshot]:
        """Hold the barrier while a future selector publication is attempted."""
        if self._session_store is None:
            raise SQLiteAuthorityError("selector publication executor is not bound")
        with self._session_store.operation_lock():
            snapshot = self._snapshot_locked()
            self._check_selector_binding(
                snapshot, selector_ref, expected_state_revision, barrier_id, fencing_token
            )
            yield snapshot
            self._assert_snapshot_locked(snapshot)

    def execute_generated_operation(
        self,
        operation: Mapping[str, object],
        destination: Path,
        binding: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute only the generated SQLite backup opcode under a held session.

        This is the first production operation dispatch seam.  It does not
        authorize upgrades or selector replacement; unsupported opcodes remain
        fail-closed until their corresponding lifecycle contracts exist.
        """
        operation_id, inputs = self._validate_generated_operation(operation)
        session_store = self._session_store
        if session_store is None:
            raise SQLiteAuthorityError("generated backup executor is not bound")
        snapshot = self.snapshot()
        if snapshot.control.status != "held":
            raise SQLiteAuthorityError("generated backup requires a held durable barrier")
        self._validate_generated_journal(snapshot, operation_id, inputs)
        result: dict[str, Any]
        try:
            with session_store.operation_lock():
                if self._snapshot_locked() != snapshot:
                    raise SQLiteAuthorityError("generated backup durable state changed")
                result = self._adapter.backup_bound(destination, binding)
                self._publish_generated_outcome(snapshot, operation_id, result)
        except SQLiteAuthorityError:
            raise
        except Exception as error:
            raise SQLiteAuthorityError("generated SQLite backup failed") from error
        return {
            "operation_id": operation_id,
            "opcode": "backend.backup",
            "outcome": "completed",
            **result,
        }

    @staticmethod
    def _validate_generated_operation(
        operation: Mapping[str, object],
    ) -> tuple[str, Mapping[str, object]]:
        if not isinstance(operation, Mapping):
            raise SQLiteAuthorityError("generated operation must be an object")
        if operation.get("opcode") != "backend.backup":
            raise SQLiteAuthorityError("generated SQLite operation is unsupported")
        SQLiteLifecycleExecutor._validate_generated_operation_fields(operation)
        operation_id = operation.get("operation_id")
        inputs = operation.get("inputs")
        if not isinstance(operation_id, str) or not operation_id:
            raise SQLiteAuthorityError("generated operation identity is invalid")
        required = {
            "backend",
            "selector_ref",
            "expected_state_revision",
            "barrier_id",
            "fencing_token",
            "backup_operation_id",
        }
        if not isinstance(inputs, Mapping) or set(inputs) != required:
            raise SQLiteAuthorityError("generated SQLite operation binding is invalid")
        if inputs.get("backend") != "sqlite":
            raise SQLiteAuthorityError("generated SQLite operation binding is invalid")
        if inputs.get("backup_operation_id") != operation_id:
            raise SQLiteAuthorityError("generated backup operation identity is invalid")
        if operation.get("preconditions") != ["previous-phase-complete"]:
            raise SQLiteAuthorityError("generated backup preconditions are invalid")
        if operation.get("durable_record") != "operation-id-and-outcome":
            raise SQLiteAuthorityError("generated operation durability contract is invalid")
        return operation_id, inputs

    @staticmethod
    def _validate_generated_operation_fields(operation: Mapping[str, object]) -> None:
        if set(operation) != {
            "operation_id",
            "opcode",
            "inputs",
            "timeout_seconds",
            "resources",
            "preconditions",
            "postconditions",
            "evidence",
            "durable_record",
        }:
            raise SQLiteAuthorityError("generated operation fields are incomplete or unknown")
        if operation.get("timeout_seconds") != 300:
            raise SQLiteAuthorityError("generated operation timeout is invalid")
        if operation.get("resources") != ["maintenance-barrier", "durable-operation-record"]:
            raise SQLiteAuthorityError("generated operation resources are invalid")
        if operation.get("postconditions") != ["backup-contract-satisfied"]:
            raise SQLiteAuthorityError("generated backup postconditions are invalid")
        if operation.get("evidence") != ["durable-operation-record"]:
            raise SQLiteAuthorityError("generated operation evidence is invalid")

    @staticmethod
    def _validate_generated_journal(
        snapshot: SQLiteLifecycleSnapshot,
        operation_id: str,
        inputs: Mapping[str, object],
    ) -> None:
        if snapshot.journal.phase != "backup" or not snapshot.journal.records:
            raise SQLiteAuthorityError("generated backup journal step is missing")
        journal_record = snapshot.journal.records[-1]
        if (
            journal_record.get("operation_id") != operation_id.rsplit(":", maxsplit=1)[0]
            or journal_record.get("step_id") != operation_id
            or journal_record.get("phase") != "backup"
            or journal_record.get("outcome") != "started"
        ):
            raise SQLiteAuthorityError("generated backup journal identity is invalid")
        context = journal_record.get("context")
        identity = snapshot.control.identity
        if (
            not isinstance(context, Mapping)
            or context.get("selector_ref") != inputs.get("selector_ref")
            or context.get("state_revision") != inputs.get("expected_state_revision")
            or inputs.get("expected_state_revision") != identity.state_revision
            or inputs.get("barrier_id") != identity.durable_barrier_id
            or inputs.get("fencing_token") != identity.fencing_token
            or context.get("durable_barrier_id") != identity.durable_barrier_id
            or context.get("fencing_token") != identity.fencing_token
        ):
            raise SQLiteAuthorityError("generated backup fencing or selector identity is invalid")

    def _publish_generated_outcome(
        self,
        before: SQLiteLifecycleSnapshot,
        operation_id: str,
        result: Mapping[str, Any],
    ) -> None:
        """Atomically persist the generated step outcome after its effect."""
        if self._journal is None or self._session_store is None:
            raise SQLiteAuthorityError("generated backup executor is not bound")
        lock = (
            nullcontext()
            if self._session_store.operation_owned_by_current_thread
            else self._session_store.operation_lock()
        )
        with lock:
            current = self._snapshot_locked()
            if current.control != before.control or current.journal != before.journal:
                raise SQLiteAuthorityError("generated backup durable state changed")
            document = json.loads(self._journal.read_text(encoding="utf-8"))
            records = document.get("records")
            if not isinstance(records, list) or not records:
                raise SQLiteAuthorityError("generated backup journal records are invalid")
            record = records[-1]
            if record.get("step_id") != operation_id or record.get("outcome") != "started":
                raise SQLiteAuthorityError("generated backup journal step changed")
            record["outcome"] = "success"
            record["result"] = dict(result)
            descriptor, temporary = tempfile.mkstemp(
                prefix=".upgrade-journal-", suffix=".json", dir=self._journal.parent
            )
            temporary_path = Path(temporary)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(json.dumps(document, sort_keys=True) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary_path.replace(self._journal)
                directory = os.open(self._journal.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError as error:
                raise SQLiteAuthorityError("generated backup outcome publication failed") from error
            finally:
                temporary_path.unlink(missing_ok=True)

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
            value = path.lstat()
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
            parent = self._authority.parent.lstat()
            descriptor = self._authority.lstat()
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
