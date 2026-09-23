# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Bounded Git authority commit capability.

The capability is intentionally not wired into the public upgrade dispatcher.
It is the independently testable effect seam for the Git mutation gate.
"""

# The executable and arguments are fixed by the capability contract.

from __future__ import annotations

import re
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from tools.authority_mutation import (
    AuthorityMutationAmbiguousError,
    AuthorityMutationRejectedError,
)
from tools.authority_neutral_commit import CommitAdmissionBundle


class GitMutationError(RuntimeError):
    """A Git authority effect was rejected or its outcome is ambiguous."""


class GitMutationRejectedError(GitMutationError, AuthorityMutationRejectedError):
    """Git admission was rejected before invoking the commit effect."""


class GitMutationAmbiguousError(GitMutationError, AuthorityMutationAmbiguousError):
    """The Git process outcome cannot be classified as success or failure."""


_SAFE_MESSAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,127}\Z")
_COMMIT_HEAD = re.compile(r"\[[^\]]+\s+([0-9a-f]{7,64})\]")
_Runner = Callable[..., subprocess.CompletedProcess[str]]
_AdmissionReread = Callable[[], Mapping[str, object]]


@dataclass(frozen=True)
class GitCommitResult:
    """Verified result of one exact-head Git commit."""

    backend: str
    target: str
    operation_id: str
    state_revision: int
    barrier_id: str
    artifact_identity: str
    manifest_identity: str
    selector_identity: str
    runtime_identity: str
    before_head: str
    after_head: str
    branch: str
    fencing_token: str
    mutates_authority: bool = True


class GitCommitCapability:
    """Commit one pre-staged authority change under an immutable identity."""

    def __init__(
        self,
        repository: Path,
        *,
        admission: CommitAdmissionBundle,
        admission_reread: _AdmissionReread,
        expected_branch: str,
        expected_head: str,
        runner: _Runner = subprocess.run,
    ) -> None:
        self._repository = repository.absolute()
        self._repository_identity = self._read_repository_identity(self._repository)
        if admission.backend != "git" or admission.target != "new":
            raise GitMutationError("Git mutation admission identity is invalid")
        self._admission = admission
        if not callable(admission_reread):
            raise GitMutationError("Git admission reread is invalid")
        self._admission_reread = admission_reread
        self._operation_id = self._validate_text(admission.operation_id, "operation identity")
        self._fencing_token = self._validate_text(admission.fencing_token, "fencing token")
        self._expected_branch = self._validate_text(expected_branch, "branch identity")
        self._expected_head = self._validate_head(expected_head)
        self._runner = runner
        self._consumed = False

    @staticmethod
    def _read_repository_identity(repository: Path) -> tuple[int, int]:
        try:
            status = repository.lstat()
        except OSError as error:
            raise GitMutationError("Git authority repository is unavailable") from error
        if not stat.S_ISDIR(status.st_mode):
            raise GitMutationError("Git authority repository is not a regular directory")
        return status.st_dev, status.st_ino

    def _assert_repository_identity(self) -> None:
        try:
            current = self._read_repository_identity(self._repository)
        except GitMutationError as error:
            raise GitMutationRejectedError(
                "Git authority repository identity changed before commit"
            ) from error
        if current != self._repository_identity:
            raise GitMutationRejectedError(
                "Git authority repository identity changed before commit"
            )

    @staticmethod
    def _validate_text(value: str, label: str) -> str:
        if not isinstance(value, str) or not value or not _SAFE_MESSAGE.fullmatch(value):
            raise GitMutationError(f"Git {label} is invalid")
        return value

    @staticmethod
    def _validate_head(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40,64}", value):
            raise GitMutationError("Git head identity is invalid")
        return value

    def _git(self, *args: str) -> str:
        try:
            result = self._runner(
                ["git", "-C", str(self._repository), *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GitMutationRejectedError("Git identity reread was rejected") from error
        if result.returncode != 0:
            raise GitMutationRejectedError("Git identity reread was rejected")
        return result.stdout.rstrip("\n")

    @staticmethod
    def _commit_head(result: subprocess.CompletedProcess[str]) -> str:
        match = _COMMIT_HEAD.search(f"{result.stdout}\n{result.stderr}")
        if match is None:
            raise GitMutationAmbiguousError("Git commit identity is unavailable")
        return match.group(1)

    def _assert_before(self) -> tuple[str, str]:
        self._assert_repository_identity()
        branch = self._git("symbolic-ref", "--short", "-q", "HEAD")
        head = self._git("rev-parse", "--verify", "HEAD^{commit}")
        if branch != self._expected_branch or head != self._expected_head:
            raise GitMutationRejectedError("Git authority identity changed before commit")
        status = self._git("status", "--porcelain=v1", "--untracked-files=all")
        lines = [line for line in status.splitlines() if line]
        if any(len(line) < 2 or line[1] != " " for line in lines):
            raise GitMutationRejectedError("Git authority has unstaged or untracked changes")
        if not lines:
            raise GitMutationRejectedError("Git authority has no staged change")
        return branch, head

    def _assert_admission_current(self) -> None:
        try:
            current = self._admission_reread()
        except Exception as error:
            raise GitMutationRejectedError("Git admission reread was rejected") from error
        if not isinstance(current, Mapping) or not self._admission.matches(current):
            raise GitMutationRejectedError("Git admission identity changed before commit")

    def commit(self, message: str) -> GitCommitResult:
        if self._consumed:
            raise GitMutationError("Git mutation capability already consumed")
        message = self._validate_text(message, "commit message")
        branch, before = self._assert_before()
        self._assert_admission_current()
        self._consumed = True
        try:
            result = self._runner(
                ["git", "-C", str(self._repository), "commit", "--no-verify", "-m", message],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GitMutationAmbiguousError("Git commit outcome is ambiguous") from error
        if result.returncode != 0:
            # A nonzero exit does not prove that Git made no ref update.  The
            # caller must fence the operation and recover/reconcile before a
            # fresh capability can be issued.
            raise GitMutationAmbiguousError("Git commit outcome is ambiguous")
        committed_head = self._commit_head(result)
        try:
            after_branch = self._git("symbolic-ref", "--short", "-q", "HEAD")
            after = self._git("rev-parse", "--verify", "HEAD^{commit}")
            status = self._git("status", "--porcelain=v1", "--untracked-files=all")
        except GitMutationAmbiguousError:
            raise
        except GitMutationError as error:
            raise GitMutationAmbiguousError("Git commit postcondition is ambiguous") from error
        if (
            after_branch != branch
            or after == before
            or not after.startswith(committed_head)
            or status
        ):
            raise GitMutationAmbiguousError("Git commit postcondition is ambiguous")
        return GitCommitResult(
            backend=self._admission.backend,
            target=self._admission.target,
            operation_id=self._operation_id,
            state_revision=self._admission.state_revision,
            barrier_id=self._admission.barrier_id,
            artifact_identity=self._admission.artifact_identity,
            manifest_identity=self._admission.manifest_identity,
            selector_identity=self._admission.selector_identity,
            runtime_identity=self._admission.runtime_identity,
            before_head=before,
            after_head=self._validate_head(after),
            branch=branch,
            fencing_token=self._fencing_token,
        )
