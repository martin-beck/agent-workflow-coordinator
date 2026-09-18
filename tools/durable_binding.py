# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Authority-neutral durable identity contracts for future upgrade adapters.

This module is deliberately an admission/read-only seam.  It does not dispatch
upgrade operations or make selector, replacement, or rollback mutation
executable.
"""

from __future__ import annotations

import stat
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

AuthorityKind = Literal["git", "sqlite"]


class DurableBindingError(RuntimeError):
    """Durable authority or session identity cannot be proven."""


@dataclass(frozen=True, slots=True)
class AuthorityIdentity:
    """Stable filesystem identity captured for one authority path."""

    kind: AuthorityKind
    path: Path
    parent: tuple[int, int]
    descriptor: tuple[int, int, int, int, int]

    @classmethod
    def capture(cls, path: Path, kind: AuthorityKind) -> AuthorityIdentity:
        if not isinstance(path, Path) or kind not in {"git", "sqlite"}:
            raise DurableBindingError("authority identity input is invalid")
        try:
            parent_status = path.parent.lstat()
            status = path.lstat()
        except OSError as error:
            raise DurableBindingError("authority identity is unavailable") from error
        if stat.S_ISLNK(parent_status.st_mode) or stat.S_ISLNK(status.st_mode):
            raise DurableBindingError("authority identity must not use symlinks")
        if kind == "sqlite" and not stat.S_ISREG(status.st_mode):
            raise DurableBindingError("SQLite authority must be a regular file")
        if kind == "git" and not stat.S_ISDIR(status.st_mode):
            raise DurableBindingError("Git authority must be a directory")
        if not stat.S_ISDIR(parent_status.st_mode):
            raise DurableBindingError("authority parent must be a directory")
        return cls(
            kind,
            path.absolute(),
            (parent_status.st_dev, parent_status.st_ino),
            (
                status.st_dev,
                status.st_ino,
                status.st_uid,
                stat.S_IMODE(status.st_mode),
                status.st_nlink,
            ),
        )

    def reread(self) -> AuthorityIdentity:
        """Capture and compare the same authority identity."""
        current = type(self).capture(self.path, self.kind)
        if current != self:
            raise DurableBindingError("authority identity changed")
        return current


@runtime_checkable
class DurableAuthorityBinding(Protocol):
    """Authority-neutral identity surface required by a future executor."""

    @property
    def identity(self) -> AuthorityIdentity: ...

    def assert_current(self) -> AuthorityIdentity: ...


@dataclass(frozen=True, slots=True)
class FilesystemAuthorityBinding:
    """Concrete identity-only binding for Git or SQLite authority paths."""

    identity: AuthorityIdentity

    @classmethod
    def bind(cls, path: Path, kind: AuthorityKind) -> FilesystemAuthorityBinding:
        return cls(AuthorityIdentity.capture(path, kind))

    def assert_current(self) -> AuthorityIdentity:
        return self.identity.reread()


@runtime_checkable
class DurableUpgradeSession(Protocol):
    """Read-only session surface shared by future Git and SQLite adapters."""

    @property
    def binding(self) -> DurableAuthorityBinding: ...

    def assert_current(self) -> AuthorityIdentity: ...

    def operation_lock(self) -> AbstractContextManager[None]: ...

    def record_outcome(self, operation_id: str, result: object) -> None: ...


@runtime_checkable
class _SQLiteSessionStore(Protocol):
    @property
    def authority_path(self) -> Path | None: ...

    def operation_lock(self) -> AbstractContextManager[None]: ...


class SQLiteCompatibilitySession:
    """Read-only compatibility wrapper around the existing SQLite session.

    Outcome publication is intentionally unavailable here; enabling it belongs
    to a separately reviewed generated-operation adapter slice.
    """

    def __init__(
        self, binding: FilesystemAuthorityBinding, session_store: _SQLiteSessionStore
    ) -> None:
        if binding.identity.kind != "sqlite":
            raise DurableBindingError("SQLite compatibility requires SQLite identity")
        authority_path = session_store.authority_path
        if authority_path != binding.identity.path:
            raise DurableBindingError("SQLite session is bound to a foreign authority")
        self.binding = binding
        self._session_store = session_store

    def assert_current(self) -> AuthorityIdentity:
        return self.binding.assert_current()

    def operation_lock(self) -> AbstractContextManager[None]:
        self.assert_current()
        return self._session_store.operation_lock()

    def record_outcome(self, _operation_id: str, _result: object) -> None:
        raise DurableBindingError("durable outcome publication is not enabled")
