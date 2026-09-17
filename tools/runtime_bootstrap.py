# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Stable, read-only resolution of the authenticated runtime selector."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from tools.upgrade_authority import AuthorityError, read_runtime_selector

_RELEASE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_OID = re.compile(r"[0-9a-f]{40}\Z")
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MANIFEST_FIELDS = {
    "release",
    "source_commit",
    "tag_ref",
    "tag_object",
    "signature_sha256",
    "trust_policy_sha256",
    "vendor_manifest_sha256",
}


@dataclass(frozen=True, slots=True)
class ExpectedRuntimeIdentity:
    """Caller-supplied identity facts validated independently."""

    source_commit: str
    tag_ref: str
    tag_object: str
    signature_sha256: str
    trust_policy_sha256: str
    vendor_manifest_sha256: str


@dataclass(frozen=True, slots=True)
class VerifiedManifest:
    """Manifest identity retained as one verification result."""

    release: str
    identity: ExpectedRuntimeIdentity
    digest: str


@dataclass(frozen=True, slots=True)
class DispatchAdmission:
    """Read-only admission evidence bound to one retained runtime handle."""

    runtime: ResolvedRuntime
    identity: VerifiedManifest

    def __post_init__(self) -> None:
        """Reject forged evidence pairs before they can reach a consumer."""
        runtime_candidate: object = self.runtime
        identity_candidate: object = self.identity
        runtime = runtime_candidate if isinstance(runtime_candidate, ResolvedRuntime) else None
        if runtime is None or not isinstance(identity_candidate, VerifiedManifest):
            if runtime is not None:
                runtime.close()
            raise AuthorityError("dispatch admission identity is not bound")
        if runtime.identity is not self.identity:
            runtime.close()
            raise AuthorityError("dispatch admission identity is not bound")

    def revalidate(self) -> None:
        """Recheck the retained handle before a consumer uses admission evidence."""
        runtime_candidate: object = self.runtime
        if not isinstance(runtime_candidate, ResolvedRuntime):
            raise AuthorityError("dispatch admission runtime is not retained")
        runtime = runtime_candidate
        try:
            identity_candidate: object = runtime.identity
            if (
                not isinstance(identity_candidate, VerifiedManifest)
                or identity_candidate is not self.identity
            ):
                raise AuthorityError("dispatch admission identity is not bound")
            runtime.revalidate_for_dispatch()
        except AuthorityError:
            runtime.close()
            raise

    def validate_identity(self, expected: VerifiedManifest) -> None:
        """Require the consumer's identity evidence to match this admission."""
        if not isinstance(expected, VerifiedManifest) or expected is not self.identity:
            self.runtime.close()
            raise AuthorityError("dispatch admission identity is not bound")
        self.revalidate()

    def close(self) -> None:
        """Release the retained runtime handle; repeated close is harmless."""
        self.runtime.close()

    def __enter__(self) -> DispatchAdmission:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(slots=True)
class ResolvedRuntime:
    """Opaque descriptor-bound runtime result; dispatch is intentionally absent."""

    path: Path
    descriptor: int
    identity: VerifiedManifest
    _directory_identity: tuple[int, int, int, int, int]

    def revalidate(self) -> None:
        """Fail closed if the retained directory or its pathname was replaced."""
        try:
            retained = os.fstat(self.descriptor)
            current = self.path.lstat()
        except OSError as error:
            raise AuthorityError("resolved runtime is unavailable") from error
        observed = (
            retained.st_dev,
            retained.st_ino,
            stat.S_IMODE(retained.st_mode),
            retained.st_uid,
            retained.st_nlink,
        )
        named = (
            current.st_dev,
            current.st_ino,
            stat.S_IMODE(current.st_mode),
            current.st_uid,
            current.st_nlink,
        )
        if observed != self._directory_identity or named != self._directory_identity:
            raise AuthorityError("resolved runtime identity changed")

    def revalidate_manifest(self) -> None:
        """Recheck retained manifest bytes and identity before a future dispatch."""
        manifest = read_runtime_manifest(self.path)
        identity = ExpectedRuntimeIdentity(
            manifest["source_commit"],
            manifest["tag_ref"],
            manifest["tag_object"],
            manifest["signature_sha256"],
            manifest["trust_policy_sha256"],
            manifest["vendor_manifest_sha256"],
        )
        if manifest["release"] != self.identity.release or identity != self.identity.identity:
            raise AuthorityError("resolved runtime manifest identity changed")
        verify_runtime_manifest(self.path, self.identity.digest)

    def revalidate_for_dispatch(self) -> None:
        """Run the complete retained identity gate before future dispatch."""
        self.revalidate()
        self.revalidate_manifest()

    def admit_for_dispatch(self) -> DispatchAdmission:
        """Return admission evidence only after the complete identity gate."""
        self.revalidate_for_dispatch()
        return DispatchAdmission(self, self.identity)

    def close(self) -> None:
        """Close the retained descriptor; no execution operation is exposed."""
        if self.descriptor >= 0:
            descriptor = self.descriptor
            self.descriptor = -1
            with suppress(OSError):
                os.close(descriptor)

    def __enter__(self) -> ResolvedRuntime:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _manifest_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise AuthorityError("runtime manifest contains duplicate fields")
    return value


def read_runtime_manifest(runtime_root: Path) -> dict[str, str]:
    """Read and strictly validate a runtime manifest through one descriptor."""
    manifest = runtime_root / "runtime-manifest.json"
    try:
        descriptor = os.open(manifest, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            value = os.fstat(descriptor)
            if (
                not stat.S_ISREG(value.st_mode)
                or value.st_uid != os.geteuid()
                or value.st_nlink != 1
                or stat.S_IMODE(value.st_mode) != 0o600
            ):
                raise AuthorityError("runtime manifest is unsafe")
            data = bytearray()
            while chunk := os.read(descriptor, 65536):
                data.extend(chunk)
                if len(data) > _MAX_MANIFEST_BYTES:
                    raise AuthorityError("runtime manifest is too large")
        finally:
            os.close(descriptor)
    except OSError as error:
        raise AuthorityError("runtime manifest is unavailable") from error
    try:
        parsed = json.loads(bytes(data), object_pairs_hook=_manifest_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuthorityError("runtime manifest JSON is invalid") from error
    if not isinstance(parsed, dict) or set(parsed) != _MANIFEST_FIELDS:
        raise AuthorityError("runtime manifest fields are invalid")
    if any(not isinstance(item, str) for item in parsed.values()):
        raise AuthorityError("runtime manifest identity is invalid")
    result = {key: str(parsed[key]) for key in _MANIFEST_FIELDS}
    if (
        _RELEASE.fullmatch(result["release"]) is None
        or result["tag_ref"] != f"refs/tags/{result['release']}"
        or _OID.fullmatch(result["source_commit"]) is None
        or _OID.fullmatch(result["tag_object"]) is None
        or any(
            _DIGEST.fullmatch(result[key]) is None
            for key in _MANIFEST_FIELDS - {"release", "source_commit", "tag_ref", "tag_object"}
        )
    ):
        raise AuthorityError("runtime manifest identity is invalid")
    return result


def verify_runtime_manifest(runtime_root: Path, expected_digest: str) -> bool:
    """Verify the owner-only manifest digest for one staged runtime."""
    if not isinstance(expected_digest, str) or _DIGEST.fullmatch(expected_digest) is None:
        raise AuthorityError("runtime manifest digest is invalid")
    manifest = runtime_root / "runtime-manifest.json"
    try:
        parent_before = manifest.parent.lstat()
        value = manifest.lstat()
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_uid != os.geteuid()
            or value.st_nlink != 1
            or stat.S_IMODE(value.st_mode) != 0o600
        ):
            raise AuthorityError("runtime manifest is unsafe")
        descriptor = os.open(manifest, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(descriptor)
            if (
                (opened.st_dev, opened.st_ino) != (value.st_dev, value.st_ino)
                or opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_nlink != 1
            ):
                raise AuthorityError("runtime manifest identity changed")
            hasher = sha256()
            total = 0
            while chunk := os.read(descriptor, 65536):
                total += len(chunk)
                if total > _MAX_MANIFEST_BYTES:
                    raise AuthorityError("runtime manifest is too large")
                hasher.update(chunk)
            digest = hasher.hexdigest()
        finally:
            os.close(descriptor)
        parent_after = manifest.parent.lstat()
        if (parent_before.st_dev, parent_before.st_ino) != (
            parent_after.st_dev,
            parent_after.st_ino,
        ):
            raise AuthorityError("runtime manifest parent identity changed")
    except OSError as error:
        raise AuthorityError("runtime manifest is unavailable") from error
    if digest != expected_digest:
        raise AuthorityError("runtime manifest digest does not match")
    return True


def _release_path(root: Path, release: str) -> Path:
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
    return release_path


def resolve_selected_runtime(
    selector: Path,
    releases_root: Path,
    expected_identity: ExpectedRuntimeIdentity | None = None,
    verify_authenticity: Callable[[Path, ExpectedRuntimeIdentity], VerifiedManifest] | None = None,
) -> Path:
    """Resolve one selected release without executing or mutating anything.

    The selector and release directory must be owner-only regular objects.  A
    release token is bounded to one direct child of ``releases_root``; symlink
    and hard-link aliases are rejected before a caller can execute the result.
    """
    selected = read_runtime_selector(selector)
    if expected_identity is None or verify_authenticity is None:
        raise AuthorityError("runtime authenticity verifier and expected identity are required")
    release = selected["active_release"]
    if not isinstance(release, str) or _RELEASE.fullmatch(release) is None:
        raise AuthorityError("runtime selector release identity is invalid")
    root = releases_root.absolute()
    release_path = _release_path(root, release)
    manifest = read_runtime_manifest(release_path)
    if manifest["release"] != release:
        raise AuthorityError("runtime manifest release does not match selector")
    manifest_identity = ExpectedRuntimeIdentity(
        source_commit=manifest["source_commit"],
        tag_ref=manifest["tag_ref"],
        tag_object=manifest["tag_object"],
        signature_sha256=manifest["signature_sha256"],
        trust_policy_sha256=manifest["trust_policy_sha256"],
        vendor_manifest_sha256=manifest["vendor_manifest_sha256"],
    )
    if manifest_identity != expected_identity:
        raise AuthorityError("runtime manifest identity does not match expected identity")
    try:
        verified = verify_authenticity(release_path, expected_identity)
    except Exception as error:
        raise AuthorityError("runtime authenticity verification failed") from error
    if (
        not isinstance(verified, VerifiedManifest)
        or verified.release != release
        or verified.identity != expected_identity
        or _DIGEST.fullmatch(verified.digest) is None
    ):
        raise AuthorityError("runtime authenticity evidence is not bound to selected release")
    verify_runtime_manifest(release_path, verified.digest)
    return release_path


def resolve_selected_runtime_bound(  # noqa: C901
    selector: Path,
    releases_root: Path,
    expected_identity: ExpectedRuntimeIdentity,
    verify_authenticity: Callable[[Path, ExpectedRuntimeIdentity], VerifiedManifest],
) -> ResolvedRuntime:
    """Resolve and retain the selected release directory without dispatching it."""
    selected = read_runtime_selector(selector)
    release = selected["active_release"]
    if not isinstance(release, str) or _RELEASE.fullmatch(release) is None:
        raise AuthorityError("runtime selector release identity is invalid")
    release_path = _release_path(releases_root.absolute(), release)
    try:
        descriptor = os.open(release_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        status = os.fstat(descriptor)
        directory_identity = (
            status.st_dev,
            status.st_ino,
            stat.S_IMODE(status.st_mode),
            status.st_uid,
            status.st_nlink,
        )
        if (
            not stat.S_ISDIR(status.st_mode)
            or directory_identity[2] != 0o700
            or directory_identity[3] != os.geteuid()
        ):
            raise AuthorityError("selected runtime release is unsafe")
        manifest = read_runtime_manifest(release_path)
        if manifest["release"] != release:
            raise AuthorityError("runtime manifest release does not match selector")
        manifest_identity = ExpectedRuntimeIdentity(
            manifest["source_commit"],
            manifest["tag_ref"],
            manifest["tag_object"],
            manifest["signature_sha256"],
            manifest["trust_policy_sha256"],
            manifest["vendor_manifest_sha256"],
        )
        if manifest_identity != expected_identity:
            raise AuthorityError("runtime manifest identity does not match expected identity")
        verified = verify_authenticity(release_path, expected_identity)
        if (
            not isinstance(verified, VerifiedManifest)
            or verified.release != release
            or verified.identity != expected_identity
            or _DIGEST.fullmatch(verified.digest) is None
        ):
            raise AuthorityError("runtime authenticity evidence is not bound to selected release")
        verify_runtime_manifest(release_path, verified.digest)
        result = ResolvedRuntime(release_path, descriptor, verified, directory_identity)
        result.revalidate()
        return result
    except AuthorityError:
        if "descriptor" in locals():
            os.close(descriptor)
        raise
    except OSError as error:
        if "descriptor" in locals():
            os.close(descriptor)
        raise AuthorityError("resolved runtime is unavailable") from error
