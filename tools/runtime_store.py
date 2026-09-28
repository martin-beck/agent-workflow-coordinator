"""Offline, immutable versioned-runtime staging and trust-policy checks.

This module deliberately has no selector or public upgrade command side effect.
It prepares a complete release for a later, separately admitted publication.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .runtime_bootstrap import ExpectedRuntimeIdentity, read_runtime_manifest

_RELEASE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class RuntimeStoreError(RuntimeError):
    """A release cannot be staged or trusted safely."""


@dataclass(frozen=True, slots=True)
class RuntimeTrustPolicy:
    """Immutable allowlist and identity facts for one release."""

    identity: ExpectedRuntimeIdentity
    files: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        names = [name for name, _ in self.files]
        if names != sorted(names) or len(names) != len(set(names)):
            raise RuntimeStoreError("trust policy file allowlist is not canonical")
        if any(
            not name
            or name.startswith("/")
            or ".." in Path(name).parts
            or not _DIGEST.fullmatch(digest)
            for name, digest in self.files
        ):
            raise RuntimeStoreError("trust policy file allowlist is invalid")

    def digest(self) -> str:
        """Return the canonical digest bound into the runtime identity."""
        payload = json.dumps(self.files, separators=(",", ":")).encode()
        return hashlib.sha256(payload).hexdigest()


def _require_private_ancestors(path: Path, label: str) -> None:
    """Reject symlinked ancestors and non-owner-controlled directories."""
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            value = current.lstat()
        except OSError as error:
            if current == absolute and not current.exists():
                break
            raise RuntimeStoreError(f"{label} path is unavailable") from error
        if stat.S_ISLNK(value.st_mode):
            raise RuntimeStoreError(f"{label} path contains a symlink")
        if current != absolute and not stat.S_ISDIR(value.st_mode):
            raise RuntimeStoreError(f"{label} ancestor is unsafe")
        if current != Path(current.anchor) and (
            value.st_uid not in {os.geteuid(), 0} or stat.S_IMODE(value.st_mode) & 0o022
        ):
            raise RuntimeStoreError(f"{label} ancestor is not owner-controlled")


def _digest(path: Path) -> str:
    try:
        value = path.lstat()
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_uid != os.geteuid()
            or value.st_nlink != 1
            or stat.S_IMODE(value.st_mode) not in {0o600, 0o700}
        ):
            raise RuntimeStoreError(f"runtime file is not a private regular file: {path}")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (value.st_dev, value.st_ino):
                raise RuntimeStoreError(f"runtime file changed during verification: {path}")
            hasher = hashlib.sha256()
            while chunk := os.read(descriptor, 65536):
                hasher.update(chunk)
            final = os.fstat(descriptor)
            if (final.st_dev, final.st_ino) != (value.st_dev, value.st_ino):
                raise RuntimeStoreError(f"runtime file changed during verification: {path}")
            return hasher.hexdigest()
        finally:
            os.close(descriptor)
    except OSError as error:
        raise RuntimeStoreError(f"runtime file is unavailable: {path}") from error


def _inventory(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if path.is_symlink():
                raise RuntimeStoreError("runtime release contains a symlink")
            if path.is_dir():
                continue
            name = relative.as_posix()
            result[name] = _digest(path)
    except OSError as error:
        raise RuntimeStoreError("runtime release inventory failed") from error
    return result


def verify_runtime_release(
    path: Path, policy: RuntimeTrustPolicy, *, expected_release: str | None = None
) -> None:
    """Verify exact manifest identity and the complete trusted file inventory."""
    _require_private_ancestors(path, "runtime release")
    if not path.is_dir() or path.is_symlink():
        raise RuntimeStoreError("runtime release is not a directory")
    try:
        root = path.lstat()
        if root.st_uid != os.geteuid() or stat.S_IMODE(root.st_mode) != 0o700:
            raise RuntimeStoreError("runtime release ownership or mode is unsafe")
    except OSError as error:
        raise RuntimeStoreError("runtime release is unavailable") from error
    try:
        manifest = read_runtime_manifest(path)
    except Exception as error:
        raise RuntimeStoreError("runtime manifest is invalid") from error
    expected = policy.identity
    actual_identity = ExpectedRuntimeIdentity(
        manifest["source_commit"],
        manifest["tag_ref"],
        manifest["tag_object"],
        manifest["signature_sha256"],
        manifest["trust_policy_sha256"],
        manifest["vendor_manifest_sha256"],
    )
    if (expected_release or path.name) != manifest["release"] or actual_identity != expected:
        raise RuntimeStoreError("runtime identity does not match trust policy")
    if manifest["trust_policy_sha256"] != policy.digest():
        raise RuntimeStoreError("runtime trust policy digest does not match")
    actual = _inventory(path)
    expected_files = dict(policy.files)
    expected_files["runtime-manifest.json"] = hashlib.sha256(
        (path / "runtime-manifest.json").read_bytes()
    ).hexdigest()
    if actual != expected_files:
        raise RuntimeStoreError("runtime file inventory does not match trust policy")


def stage_runtime_release(  # noqa: C901
    source: Path,
    releases_root: Path,
    release: str,
    manifest: dict[str, str],
    policy: RuntimeTrustPolicy,
) -> Path:
    """Stage one complete immutable runtime without publishing a selector."""
    if _RELEASE.fullmatch(release) is None or manifest.get("release") != release:
        raise RuntimeStoreError("runtime release identity is invalid")
    _require_private_ancestors(source, "runtime source")
    _require_private_ancestors(releases_root, "runtime release root")
    if not source.is_dir() or source.is_symlink():
        raise RuntimeStoreError("runtime source is not a directory")
    if not releases_root.exists():
        releases_root.mkdir(mode=0o700, parents=True)
    root_status = releases_root.lstat()
    if (
        root_status.st_uid != os.geteuid()
        or stat.S_IMODE(root_status.st_mode) != 0o700
        or releases_root.is_symlink()
    ):
        raise RuntimeStoreError("runtime release root is unsafe")
    destination = releases_root / release
    if destination.exists() or destination.is_symlink():
        raise RuntimeStoreError("runtime release already exists")
    with tempfile.TemporaryDirectory(prefix=f".{release}.", dir=releases_root) as temporary:
        staged = Path(temporary)
        for source_path in sorted(source.rglob("*")):
            relative = source_path.relative_to(source)
            target = staged / relative
            if source_path.is_symlink():
                raise RuntimeStoreError("runtime source contains a symlink")
            if source_path.is_dir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            source_status = source_path.lstat()
            if (
                not stat.S_ISREG(source_status.st_mode)
                or source_status.st_uid != os.geteuid()
                or source_status.st_nlink != 1
            ):
                raise RuntimeStoreError("runtime source file is not a private regular file")
            mode = 0o700 if source_status.st_mode & stat.S_IXUSR else 0o600
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(source_path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino) != (source_status.st_dev, source_status.st_ino):
                    raise RuntimeStoreError("runtime source changed during staging")
                with os.fdopen(descriptor, "rb") as source_stream:
                    descriptor = -1
                    target.write_bytes(source_stream.read())
                final = source_path.lstat()
                if (final.st_dev, final.st_ino) != (source_status.st_dev, source_status.st_ino):
                    raise RuntimeStoreError("runtime source changed during staging")
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            target.chmod(mode)
        manifest_path = staged / "runtime-manifest.json"
        if manifest_path.exists():
            raise RuntimeStoreError("runtime source must not provide the manifest")
        manifest_path.write_bytes(
            json.dumps(
                manifest,
                sort_keys=False,
                separators=(",", ":"),
            ).encode()
        )
        manifest_path.chmod(0o600)
        staged.chmod(0o700)
        verify_runtime_release(staged, policy, expected_release=release)
        staged.replace(destination)
    destination.chmod(0o700)
    return destination
