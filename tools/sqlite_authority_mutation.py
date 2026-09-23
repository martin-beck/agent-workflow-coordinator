# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Isolated, identity-bound SQLite authority commit capability."""

from __future__ import annotations

import sqlite3
import stat
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from tools.authority_neutral_commit import CommitAdmissionBundle


class SQLiteMutationError(RuntimeError):
    """A SQLite authority effect was rejected."""


class SQLiteMutationAmbiguousError(SQLiteMutationError):
    """The SQLite commit outcome cannot be classified safely."""


@dataclass(frozen=True)
class SQLiteCommitResult:
    operation_id: str
    state_revision: int
    fencing_token: str
    integrity_check: str
    foreign_key_violations: int
    mutates_authority: bool = True


_Commit = Callable[[sqlite3.Connection], None]


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
        expected_db_identity: tuple[int, int],
        expected_wal_identity: tuple[int, int] | None,
        expected_shm_identity: tuple[int, int] | None,
    ) -> None:
        if admission.backend != "sqlite" or admission.target != "new":
            raise SQLiteMutationError("SQLite mutation admission identity is invalid")
        if type(admission.state_revision) is not int or admission.state_revision < 1:
            raise SQLiteMutationError("SQLite mutation revision is invalid")
        self._authority = authority.resolve()
        self._admission = admission
        self._db_identity = expected_db_identity
        self._wal_identity = expected_wal_identity
        self._shm_identity = expected_shm_identity
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

    def commit(self, effect: _Commit) -> SQLiteCommitResult:
        if not callable(effect):
            raise SQLiteMutationError("SQLite authority effect is invalid")
        self._assert_filesystem_identity()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self._authority, isolation_level=None, timeout=5)
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            effect(connection)
            connection.commit()
        except sqlite3.Error as error:
            if connection is not None:
                with suppress(sqlite3.Error):
                    connection.rollback()
            raise SQLiteMutationAmbiguousError("SQLite commit outcome is ambiguous") from error
        except Exception:
            if connection is not None:
                with suppress(sqlite3.Error):
                    connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()
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
            operation_id=self._admission.operation_id,
            state_revision=self._admission.state_revision,
            fencing_token=self._admission.fencing_token,
            integrity_check=integrity,
            foreign_key_violations=violations,
        )
