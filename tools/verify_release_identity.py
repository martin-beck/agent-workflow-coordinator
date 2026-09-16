# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Verify release identities in a transition against an immutable Git checkout.

This is a read-only gate.  It does not fetch, checkout, install, or mutate a
repository.  The signed-tag digest is the SHA-256 of the exact ASCII signature
block stored in the annotated tag object, including its begin/end markers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
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


class ReleaseIdentityError(ValueError):
    """A transition release does not match the checked-out Git object."""


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


def _release_identity(root: Path, release: dict[str, Any]) -> None:
    version = release.get("version")
    tag_ref = release.get("tag_ref")
    if not isinstance(version, str) or not isinstance(tag_ref, str):
        raise ReleaseIdentityError("release identity is incomplete")
    expected_ref = f"refs/tags/{version}"
    if tag_ref != expected_ref or _TAG_REF.fullmatch(tag_ref) is None:
        raise ReleaseIdentityError(f"release tag reference is invalid: {tag_ref}")

    try:
        # Verify the tag's cryptographic signature against the checkout's
        # configured trust policy before accepting any identity fields.
        _git(root, "verify-tag", tag_ref)
        tag_object = str(_git(root, "rev-parse", f"{tag_ref}^{{tag}}"))
        source_commit = str(_git(root, "rev-parse", f"{tag_ref}^{{commit}}"))
        contents = _git(root, "cat-file", "tag", tag_object, text=False)
    except ReleaseIdentityError:
        raise
    if not _OID.fullmatch(tag_object) or not _OID.fullmatch(source_commit):
        raise ReleaseIdentityError(f"release {version} has an invalid Git identity")
    if tag_object != release.get("tag_object"):
        raise ReleaseIdentityError(f"release {version} tag object does not match transition")
    if source_commit != release.get("source_commit"):
        raise ReleaseIdentityError(f"release {version} source commit does not match transition")
    if not isinstance(contents, bytes):  # pragma: no cover - subprocess contract
        raise ReleaseIdentityError("release tag contents were not read as bytes")
    if _signature_digest(contents) != release.get("signature_sha256"):
        raise ReleaseIdentityError(f"release {version} signature does not match transition")


def verify_transition(root: Path, transition: dict[str, Any]) -> dict[str, str]:
    """Verify both immutable release identities in a typed transition."""
    try:
        _validate_transition(transition)
    except ContractError as error:
        raise ReleaseIdentityError(str(error)) from error
    _release_identity(root, transition["from"])
    _release_identity(root, transition["to"])
    return {
        "status": "pass",
        "from": transition["from"]["version"],
        "to": transition["to"]["version"],
    }


def _transition_path(value: Path, workspace: Path) -> Path:
    """Resolve a transition only from a non-aliased file under workspace."""
    root = workspace.resolve()
    original = root / value if not value.is_absolute() else value
    # Inspect the lexical path before resolving it.  Resolving first would
    # erase an in-workspace symlink component and make the path identity
    # dependent on mutable filesystem aliases.
    try:
        lexical_relative = original.relative_to(root)
    except ValueError as error:
        raise ReleaseIdentityError("transition path escapes workspace") from error
    current = root
    for component in lexical_relative.parts:
        current /= component
        if current.is_symlink():
            raise ReleaseIdentityError("transition path must be a regular workspace file")
    candidate = original.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ReleaseIdentityError("transition path escapes workspace") from error
    if original.is_symlink() or not candidate.is_file():
        raise ReleaseIdentityError("transition path must be a regular workspace file")
    return candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transition", type=Path)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        transition_file = _transition_path(args.transition, args.workspace)
        transition = json.loads(transition_file.read_text(encoding="utf-8"))
        print(json.dumps(verify_transition(args.repository, transition), sort_keys=True))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"invalid release identity: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
