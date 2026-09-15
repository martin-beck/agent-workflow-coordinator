"""Read-only Git authority identity snapshot for the future caller seam."""

from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tools.upgrade_authority import AuthorityError


@dataclass(frozen=True, slots=True)
class GitAuthoritySnapshot:
    """Stable facts from one clean, reachable Git authority."""

    root: Path
    head_ref: str
    head_commit: str
    release_ref: str
    release_commit: str
    git_identity: tuple[int, int]


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise AuthorityError("Git authority read failed") from error
    return result.stdout.strip()


def _identity(path: Path) -> tuple[int, int]:
    try:
        status = path.stat()
    except OSError as error:
        raise AuthorityError("Git authority descriptor is unavailable") from error
    if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.geteuid():
        raise AuthorityError("Git authority descriptor is unsafe")
    return status.st_dev, status.st_ino


def read_git_authority_snapshot(root: Path, release_ref: str) -> GitAuthoritySnapshot:
    """Read a clean Git ref and prove it is reachable from the current HEAD.

    This is deliberately uncalled by upgrade execution. It performs no Git
    mutation and rereads the repository identity before returning.
    """
    if not isinstance(root, Path) or not root.is_absolute() or root != root.resolve():
        raise AuthorityError("Git authority root is not canonical")
    if not isinstance(release_ref, str) or not release_ref.startswith(("refs/heads/", "refs/tags/")):
        raise AuthorityError("Git authority release ref is invalid")
    identity = _identity(root / ".git")
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise AuthorityError("Git authority is dirty")
    head_ref = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    head_commit = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    release_commit = _git(root, "rev-parse", "--verify", f"{release_ref}^{{commit}}")
    try:
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", release_commit, head_commit],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise AuthorityError("Git authority release ref is not reachable") from error
    if _identity(root / ".git") != identity:
        raise AuthorityError("Git authority descriptor identity changed")
    if _git(root, "rev-parse", "--verify", "HEAD^{commit}") != head_commit:
        raise AuthorityError("Git authority HEAD changed")
    return GitAuthoritySnapshot(root, head_ref, head_commit, release_ref, release_commit, identity)
