# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Isolated, identity-bound SQLite authority commit capability."""

from __future__ import annotations

import sqlite3
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from tools.authority_mutation import (
    AuthorityMutationAmbiguousError,
    AuthorityMutationRejectedError,
)
from tools.authority_neutral_commit import CommitAdmissionBundle


class SQLiteMutationError(RuntimeError):
    """A SQLite authority effect was rejected."""


class SQLiteMutationRejectedError(SQLiteMutationError, AuthorityMutationRejectedError):
    """SQLite admission was rejected before opening the effect transaction."""


class SQLiteMutationAmbiguousError(SQLiteMutationError, AuthorityMutationAmbiguousError):
    """The SQLite commit outcome cannot be classified safely."""


@dataclass(frozen=True)
class SQLiteCommitResult:
    backend: str
    target: str
    operation_id: str
    state_revision: int
    barrier_id: str
    artifact_identity: str
    manifest_identity: str
    selector_identity: str
    runtime_identity: str
    fencing_token: str
    integrity_check: str
    foreign_key_violations: int
    mutates_authority: bool = True


_Commit = Callable[[sqlite3.Connection], None]
_Connector = Callable[..., sqlite3.Connection]
_AdmissionReread = Callable[[], Mapping[str, object]]


class SQLiteCommitCapability:
    """Apply one callback inside a bound transaction and verify the result.

    The caller owns the durable barrier/lock scope. This capability only
    checks immutable admission and filesystem identities; it is not wired into
    the public upgrade dispatcher.
    """

    def __init__(
        self,
        authority: Path,
        *,
        admission: CommitAdmissionBundle,
        admission_reread: _AdmissionReread,
        expected_db_identity: tuple[int, int],
        expected_wal_identity: tuple[int, int] | None,
        expected_shm_identity: tuple[int, int] | None,
        connector: _Connector = sqlite3.connect,
    ) -> None:
        if admission.backend != "sqlite" or admission.target != "new":
            raise SQLiteMutationError("SQLite mutation admission identity is invalid")
        if type(admission.state_revision) is not int or admission.state_revision < 1:
            raise SQLiteMutationError("SQLite mutation revision is invalid")
        self._authority = authority.resolve()
        self._admission = admission
        if not callable(admission_reread):
            raise SQLiteMutationError("SQLite admission reread is invalid")
        self._admission_reread = admission_reread
        self._db_identity = expected_db_identity
        self._wal_identity = expected_wal_identity
        self._shm_identity = expected_shm_identity
        self._connector = connector
        self._consumed = False
        self._validate_identity(expected_db_identity, "database")
        self._validate_optional_identity(expected_wal_identity, "WAL")
        self._validate_optional_identity(expected_shm_identity, "SHM")

    @staticmethod
    def _validate_identity(value: tuple[int, int], label: str) -> None:
        if (
            not isinstance(value, tuple)
            or len(value) != 2
            or any(type(part) is not int or part < 0 for part in value)
        ):
            raise SQLiteMutationError(f"SQLite {label} identity is invalid")

    @classmethod
    def _validate_optional_identity(cls, value: tuple[int, int] | None, label: str) -> None:
        if value is not None:
            cls._validate_identity(value, label)

    @staticmethod
    def _identity(path: Path) -> tuple[int, int] | None:
        try:
            status = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise SQLiteMutationError("SQLite sidecar identity is unavailable") from error
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise SQLiteMutationError("SQLite authority file is not a regular private file")
        return status.st_dev, status.st_ino

    def _assert_filesystem_identity(self) -> None:
        if self._identity(self._authority) != self._db_identity:
            raise SQLiteMutationError("SQLite database identity changed")
        if (
            self._identity(self._authority.with_name(self._authority.name + "-wal"))
            != self._wal_identity
        ):
            raise SQLiteMutationError("SQLite WAL identity changed")
        if (
            self._identity(self._authority.with_name(self._authority.name + "-shm"))
            != self._shm_identity
        ):
            raise SQLiteMutationError("SQLite SHM identity changed")

    def _assert_admission_current(self) -> None:
        try:
            current = self._admission_reread()
        except Exception as error:
            raise SQLiteMutationRejectedError("SQLite admission reread was rejected") from error
        if not isinstance(current, Mapping) or not self._admission.matches(current):
            raise SQLiteMutationRejectedError("SQLite admission identity changed before commit")

    def _consume(self, effect: _Commit) -> None:
        if self._consumed:
            raise SQLiteMutationError("SQLite mutation capability already consumed")
        if not callable(effect):
            raise SQLiteMutationError("SQLite authority effect is invalid")
        self._consumed = True

    @staticmethod
    def _close_connection(connection: sqlite3.Connection) -> None:
        try:
            connection.close()
        except sqlite3.Error as error:
            raise SQLiteMutationAmbiguousError(
                "SQLite connection close outcome is ambiguous"
            ) from error

    @staticmethod
    def _rollback_connection(connection: sqlite3.Connection) -> None:
        try:
            connection.rollback()
        except sqlite3.Error as error:
            raise SQLiteMutationAmbiguousError("SQLite rollback outcome is ambiguous") from error

    def commit(self, effect: _Commit) -> SQLiteCommitResult:
        self._consume(effect)
        self._assert_filesystem_identity()
        self._assert_admission_current()
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connector(self._authority, isolation_level=None, timeout=5)
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            effect(connection)
            connection.commit()
        except sqlite3.Error as error:
            if connection is not None:
                self._rollback_connection(connection)
            raise SQLiteMutationAmbiguousError("SQLite commit outcome is ambiguous") from error
        except Exception:
            if connection is not None:
                self._rollback_connection(connection)
            raise
        finally:
            if connection is not None:
                self._close_connection(connection)
        try:
            self._assert_filesystem_identity()
            with sqlite3.connect(self._authority) as verification:
                verification.execute("PRAGMA foreign_keys=ON")
                integrity = str(verification.execute("PRAGMA integrity_check").fetchone()[0])
                violations = len(verification.execute("PRAGMA foreign_key_check").fetchall())
        except (OSError, SQLiteMutationError, sqlite3.Error) as error:
            raise SQLiteMutationAmbiguousError(
                "SQLite post-commit verification is ambiguous"
            ) from error
        if integrity != "ok" or violations:
            raise SQLiteMutationError("SQLite post-commit integrity is invalid")
        return SQLiteCommitResult(
            backend=self._admission.backend,
            target=self._admission.target,
            operation_id=self._admission.operation_id,
            state_revision=self._admission.state_revision,
            barrier_id=self._admission.barrier_id,
            artifact_identity=self._admission.artifact_identity,
            manifest_identity=self._admission.manifest_identity,
            selector_identity=self._admission.selector_identity,
            runtime_identity=self._admission.runtime_identity,
            fencing_token=self._admission.fencing_token,
            integrity_check=integrity,
            foreign_key_violations=violations,
        )
