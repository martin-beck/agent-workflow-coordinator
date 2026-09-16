# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Stable, read-only resolution of the authenticated runtime selector."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

from tools.upgrade_authority import AuthorityError, read_runtime_selector

_RELEASE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def verify_runtime_manifest(runtime_root: Path, expected_digest: str) -> bool:
    """Verify the owner-only manifest digest for one staged runtime."""
    if not isinstance(expected_digest, str) or _DIGEST.fullmatch(expected_digest) is None:
        raise AuthorityError("runtime manifest digest is invalid")
    manifest = runtime_root / "runtime-manifest.json"
    try:
        value = manifest.lstat()
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_uid != os.geteuid()
            or value.st_nlink != 1
            or stat.S_IMODE(value.st_mode) != 0o600
        ):
            raise AuthorityError("runtime manifest is unsafe")
        digest = sha256(manifest.read_bytes()).hexdigest()
    except OSError as error:
        raise AuthorityError("runtime manifest is unavailable") from error
    if digest != expected_digest:
        raise AuthorityError("runtime manifest digest does not match")
    return True


def resolve_selected_runtime(
    selector: Path,
    releases_root: Path,
    verify_authenticity: Callable[[Path], bool] | None = None,
) -> Path:
    """Resolve one selected release without executing or mutating anything.

    The selector and release directory must be owner-only regular objects.  A
    release token is bounded to one direct child of ``releases_root``; symlink
    and hard-link aliases are rejected before a caller can execute the result.
    """
    selected = read_runtime_selector(selector)
    if verify_authenticity is None:
        raise AuthorityError("runtime authenticity verifier is required")
    release = selected["active_release"]
    if not isinstance(release, str) or _RELEASE.fullmatch(release) is None:
        raise AuthorityError("runtime selector release identity is invalid")
    root = releases_root.absolute()
    try:
        component = Path(root.anchor)
        for part in root.parts[1:]:
            component /= part
            if stat.S_ISLNK(component.lstat().st_mode):
                raise AuthorityError("runtime release root contains a symlink")
        root_stat = root.stat()
        release_path = root / release
        value = release_path.lstat()
    except OSError as error:
        raise AuthorityError("selected runtime release is unavailable") from error
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != os.geteuid()
        or stat.S_IMODE(root_stat.st_mode) != 0o700
        or not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.geteuid()
        or stat.S_IMODE(value.st_mode) != 0o700
    ):
        raise AuthorityError("selected runtime release is unsafe")
    try:
        verified = verify_authenticity(release_path)
    except Exception as error:
        raise AuthorityError("runtime authenticity verification failed") from error
    if verified is not True:
        raise AuthorityError("runtime authenticity verification failed")
    return release_path
