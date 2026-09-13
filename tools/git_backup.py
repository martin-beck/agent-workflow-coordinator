# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Create and verify complete, offline Git-backend backup artifacts."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path


class BackupError(RuntimeError):
    """Raised when a backup is incomplete, corrupt, or unsafe to restore."""


def _run(args: list[str], cwd: Path) -> str:
    try:
        return subprocess.check_output(  # noqa: S603
            args, cwd=cwd, text=True, stderr=subprocess.STDOUT
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "output", "")
        raise BackupError(f"Git backup command failed: {args[0]}: {detail}") from error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_backup(repo: Path, destination: Path) -> Path:
    """Create immutable bundle/archive/manifest artifacts without mutating authority."""
    repo = repo.resolve()
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise BackupError("backup destination must be empty")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    bundle = destination / "authority.bundle"
    archive = destination / "tracked-tree.tar"
    refs = destination / "refs.txt"
    _run(["git", "bundle", "create", str(bundle), "--all"], repo)
    refs.write_text(_run(["git", "show-ref"], repo), encoding="utf-8")
    _run(["git", "archive", "--format=tar", "HEAD", "-o", str(archive)], repo)
    commit = _run(["git", "rev-parse", "HEAD"], repo).strip()
    manifest = {
        "schema_version": 1,
        "commit": commit,
        "artifacts": {
            name: {
                "sha256": _sha256(destination / name),
                "size": (destination / name).stat().st_size,
            }
            for name in ("authority.bundle", "tracked-tree.tar", "refs.txt")
        },
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return destination


def verify_backup(backup: Path) -> dict[str, object]:
    """Verify hashes, bundle reachability, and clean-checkout restore before use."""
    backup = backup.resolve()
    try:
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        artifacts = manifest["artifacts"]
        commit = manifest["commit"]
        for name, metadata in artifacts.items():
            path = backup / name
            if not path.is_file() or _sha256(path) != metadata["sha256"]:
                raise BackupError(f"backup artifact integrity failure: {name}")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise BackupError("invalid backup manifest") from error
    with tempfile.TemporaryDirectory(prefix="handoffctl-restore-") as directory:
        verifier = Path(directory) / "verifier"
        _run(["git", "init", "-q", str(verifier)], Path(directory))
        _run(["git", "bundle", "verify", str(backup / "authority.bundle")], verifier)
        target = Path(directory) / "checkout"
        _run(["git", "clone", str(backup / "authority.bundle"), str(target)], backup)
        restored = _run(["git", "rev-parse", "HEAD"], target).strip()
        if restored != commit:
            raise BackupError("clean restore commit mismatch")
        with tarfile.open(backup / "tracked-tree.tar") as archive:
            if any(member.isdev() or member.issym() or member.islnk() for member in archive):
                raise BackupError("backup archive contains unsafe special entries")
    return {"commit": commit, "verified": True, "artifact_count": len(artifacts)}


def restore_backup(backup: Path, destination: Path) -> None:
    """Verify completely, then restore into an empty destination only."""
    verify_backup(backup)
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise BackupError("restore destination must be empty")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _run(["git", "clone", str(backup / "authority.bundle"), str(destination)], backup)
