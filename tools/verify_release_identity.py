# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Verify release identities in a transition against an immutable Git checkout.

This is a read-only gate.  It does not fetch, checkout, install, or mutate a
repository. Releases are bound to immutable Git tag and commit identities;
cryptographic tag signatures are optional. For an unsigned or lightweight tag,
``signature_sha256`` is the all-zero digest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

from .generate_upgrade_contract import _validate_transition
from .validate_upgrade_contract import ContractError

_TAG_REF = re.compile(r"refs/tags/v[0-9]+\.[0-9]+\.[0-9]+")
_OID = re.compile(r"[0-9a-f]{40}")
_BEGIN = b"-----BEGIN SSH SIGNATURE-----"
_END = b"-----END SSH SIGNATURE-----"
_MAX_TRANSITION_BYTES = 1024 * 1024


class ReleaseIdentityError(ValueError):
    """A transition release does not match the checked-out Git object."""


def _require_owner_controlled(path: Path, *, label: str, directory: bool = False) -> None:
    try:
        status = path.stat()
    except OSError as error:
        raise ReleaseIdentityError(f"{label} metadata is unavailable") from error
    if (
        (directory and not stat.S_ISDIR(status.st_mode))
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) & 0o022
    ):
        raise ReleaseIdentityError(f"{label} must be owner-controlled")


def _git(root: Path, *args: str, text: bool = True) -> str | bytes:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), *args],  # noqa: S607
            check=True,
            capture_output=True,
            text=text,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ReleaseIdentityError("release Git inspection failed") from error
    # ``cat-file`` output is the signed tag object itself.  Do not strip it:
    # the trailing newline is part of the object and therefore part of the
    # signature digest.  Textual identity queries are safe to normalize.
    stdout = cast(str | bytes, result.stdout)
    return stdout if not text else cast(str, stdout).strip()


def _signature_digest(tag_contents: bytes) -> str:
    begin = tag_contents.find(_BEGIN)
    end = tag_contents.find(_END, begin + len(_BEGIN)) if begin >= 0 else -1
    if begin < 0 or end < 0:
        raise ReleaseIdentityError("release tag has no supported SSH signature")
    end += len(_END)
    return hashlib.sha256(tag_contents[begin:end]).hexdigest()


_UNSIGNED_DIGEST = "0" * 64


def _tag_signature_digest(root: Path, tag_ref: str, tag_object: str, tag_type: str) -> str:
    if tag_type != "tag":
        return _UNSIGNED_DIGEST
    try:
        _git(root, "verify-tag", tag_ref)
    except ReleaseIdentityError:
        return _UNSIGNED_DIGEST
    contents = _git(root, "cat-file", "tag", tag_object, text=False)
    if not isinstance(contents, bytes):  # pragma: no cover - subprocess contract
        raise ReleaseIdentityError("release tag contents were not read as bytes")
    if _BEGIN not in contents or _END not in contents:
        return _UNSIGNED_DIGEST
    return _signature_digest(contents)


def _release_identity(root: Path, release: dict[str, Any]) -> None:
    version = release.get("version")
    tag_ref = release.get("tag_ref")
    if not isinstance(version, str) or not isinstance(tag_ref, str):
        raise ReleaseIdentityError("release identity is incomplete")
    expected_ref = f"refs/tags/{version}"
    if tag_ref != expected_ref or _TAG_REF.fullmatch(tag_ref) is None:
        raise ReleaseIdentityError(f"release tag reference is invalid: {tag_ref}")

    # A release tag is the publication boundary. It may be a lightweight tag
    # (the supported default) or an annotated tag. Do not require a signing key
    # or trust-policy configuration; remote tag protection supplies immutability.
    tag_object = str(_git(root, "rev-parse", tag_ref))
    source_commit = str(_git(root, "rev-parse", f"{tag_ref}^{{commit}}"))
    tag_type = str(_git(root, "cat-file", "-t", tag_ref))
    if not _OID.fullmatch(tag_object) or not _OID.fullmatch(source_commit):
        raise ReleaseIdentityError(f"release {version} has an invalid Git identity")
    if tag_object != release.get("tag_object"):
        raise ReleaseIdentityError(f"release {version} tag object does not match transition")
    if source_commit != release.get("source_commit"):
        raise ReleaseIdentityError(f"release {version} source commit does not match transition")
    signature_digest = _tag_signature_digest(root, tag_ref, tag_object, tag_type)
    if signature_digest != release.get("signature_sha256"):
        raise ReleaseIdentityError(f"release {version} signature does not match transition")


def verify_transition(
    root: Path, transition: dict[str, Any], *, candidate: bool = False
) -> dict[str, str]:
    """Verify both immutable release identities in a typed transition."""
    try:
        _validate_transition(transition)
    except ContractError as error:
        raise ReleaseIdentityError(str(error)) from error
    _release_identity(root, transition["from"])
    if candidate:
        target = transition["to"]
        if (
            not _OID.fullmatch(str(target.get("source_commit")))
            or str(_git(root, "rev-parse", "HEAD")) != target["source_commit"]
        ):
            raise ReleaseIdentityError("candidate source commit does not match checked-out HEAD")
        if _git(root, "tag", "--list", str(target["version"])):
            raise ReleaseIdentityError("candidate target tag already exists")
    else:
        _release_identity(root, transition["to"])
    return {
        "status": "pass",
        "from": transition["from"]["version"],
        "to": transition["to"]["version"],
    }


def _validate_transition_file(candidate: Path) -> None:
    _require_owner_controlled(candidate, label="transition path")
    try:
        status = candidate.stat()
        if status.st_nlink != 1:
            raise ReleaseIdentityError("transition path must not be hard-linked")
        if status.st_size > _MAX_TRANSITION_BYTES:
            raise ReleaseIdentityError("transition path exceeds size limit")
    except OSError as error:
        raise ReleaseIdentityError("transition path metadata is unavailable") from error


def _transition_path(value: Path, workspace: Path) -> Path:
    """Resolve a transition only from a non-aliased file under workspace."""
    root = workspace.resolve()
    _require_owner_controlled(root, label="workspace", directory=True)
    original = root / value if not value.is_absolute() else value
    # Inspect the lexical path before resolving it.  Resolving first would
    # erase an in-workspace symlink component and make the path identity
    # dependent on mutable filesystem aliases.
    try:
        lexical_relative = original.relative_to(root)
    except ValueError as error:
        raise ReleaseIdentityError("transition path escapes workspace") from error
    current = root
    components = lexical_relative.parts
    for index, component in enumerate(components):
        current /= component
        if current.is_symlink():
            raise ReleaseIdentityError("transition path must be a regular workspace file")
        if index < len(components) - 1:
            _require_owner_controlled(current, label="transition parent", directory=True)
    candidate = original.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ReleaseIdentityError("transition path escapes workspace") from error
    if original.is_symlink() or not candidate.is_file():
        raise ReleaseIdentityError("transition path must be a regular workspace file")
    _validate_transition_file(candidate)
    return candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transition", type=Path)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--candidate", action="store_true", help="validate an unsigned target candidate"
    )
    args = parser.parse_args(argv)
    try:
        transition_file = _transition_path(args.transition, args.workspace)
        transition = json.loads(transition_file.read_text(encoding="utf-8"))
        print(
            json.dumps(
                verify_transition(args.repository, transition, candidate=args.candidate),
                sort_keys=True,
            )
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"invalid release identity: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
