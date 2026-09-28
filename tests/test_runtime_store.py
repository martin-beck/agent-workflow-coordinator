# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Tests for offline versioned-runtime staging."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.runtime_bootstrap import ExpectedRuntimeIdentity
from tools.runtime_store import (
    RuntimeStoreError,
    RuntimeTrustPolicy,
    stage_runtime_release,
    verify_runtime_release,
)


class RuntimeStoreTests(unittest.TestCase):
    def _identity(self) -> ExpectedRuntimeIdentity:
        return ExpectedRuntimeIdentity(
            "a" * 40,
            "refs/tags/v1.2.3",
            "b" * 40,
            "c" * 64,
            "d" * 64,
            "e" * 64,
        )

    def test_stage_and_verify_complete_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            source.chmod(0o700)
            (source / "tools").mkdir()
            (source / "tools/handoffctl.py").write_text("print('ok')\n")
            identity = self._identity()
            policy_files = (("tools/handoffctl.py", hashlib.sha256(b"print('ok')\n").hexdigest()),)
            identity = ExpectedRuntimeIdentity(
                identity.source_commit,
                identity.tag_ref,
                identity.tag_object,
                identity.signature_sha256,
                hashlib.sha256(
                    json.dumps(policy_files, separators=(",", ":")).encode()
                ).hexdigest(),
                identity.vendor_manifest_sha256,
            )
            manifest = {
                "release": "v1.2.3",
                "source_commit": identity.source_commit,
                "tag_ref": identity.tag_ref,
                "tag_object": identity.tag_object,
                "signature_sha256": identity.signature_sha256,
                "trust_policy_sha256": identity.trust_policy_sha256,
                "vendor_manifest_sha256": identity.vendor_manifest_sha256,
            }
            policy = RuntimeTrustPolicy(identity, policy_files)
            staged = stage_runtime_release(source, root / "releases", "v1.2.3", manifest, policy)
            verify_runtime_release(staged, policy)
            self.assertEqual(0o700, staged.stat().st_mode & 0o777)

    def test_symlink_source_rejected_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            source.chmod(0o700)
            (source / "escape").symlink_to("/etc/passwd")
            identity = self._identity()
            manifest = {"release": "v1.2.3", "source_commit": identity.source_commit}
            policy = RuntimeTrustPolicy(identity, ())
            with self.assertRaisesRegex(RuntimeStoreError, "symlink"):
                stage_runtime_release(source, root / "releases", "v1.2.3", manifest, policy)
            self.assertFalse((root / "releases/v1.2.3").exists())

    def test_hard_link_source_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            source.chmod(0o700)
            original = root / "original"
            original.write_text("shared")
            (source / "runtime.py").hardlink_to(original)
            identity = self._identity()
            with self.assertRaisesRegex(RuntimeStoreError, "private regular file"):
                stage_runtime_release(
                    source,
                    root / "releases",
                    "v1.2.3",
                    {"release": "v1.2.3"},
                    RuntimeTrustPolicy(identity, ()),
                )

    def test_policy_rejects_noncanonical_and_invalid_entries(self) -> None:
        identity = self._identity()
        with self.assertRaisesRegex(RuntimeStoreError, "not canonical"):
            RuntimeTrustPolicy(identity, (("z", "a" * 64), ("a", "b" * 64)))
        with self.assertRaisesRegex(RuntimeStoreError, "invalid"):
            RuntimeTrustPolicy(identity, (("../escape", "a" * 64),))
        with self.assertRaisesRegex(RuntimeStoreError, "invalid"):
            RuntimeTrustPolicy(identity, (("safe", "not-a-digest"),))

    def test_stage_rejects_invalid_identity_and_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir(mode=0o700)
            identity = self._identity()
            policy = RuntimeTrustPolicy(identity, ())
            with self.assertRaisesRegex(RuntimeStoreError, "identity"):
                stage_runtime_release(source, root / "releases", "bad", {}, policy)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            (releases / "v1.2.3").mkdir(mode=0o700)
            with self.assertRaisesRegex(RuntimeStoreError, "already exists"):
                stage_runtime_release(
                    source,
                    releases,
                    "v1.2.3",
                    {"release": "v1.2.3"},
                    policy,
                )

    def test_stage_rejects_unsafe_root_and_source_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_parent = root / "writable"
            source_parent.mkdir(mode=0o700)
            source_parent.chmod(0o777)
            source = source_parent / "source"
            source.mkdir(mode=0o700)
            identity = self._identity()
            with self.assertRaisesRegex(RuntimeStoreError, "owner-controlled"):
                stage_runtime_release(
                    source,
                    root / "releases",
                    "v1.2.3",
                    {"release": "v1.2.3"},
                    RuntimeTrustPolicy(identity, ()),
                )
            safe_source = root / "safe-source"
            safe_source.mkdir(mode=0o700)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            releases.chmod(0o755)
            with self.assertRaisesRegex(RuntimeStoreError, "release root"):
                stage_runtime_release(
                    safe_source,
                    releases,
                    "v1.2.3",
                    {"release": "v1.2.3"},
                    RuntimeTrustPolicy(identity, ()),
                )

    def test_verify_rejects_manifest_identity_policy_and_file_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir(mode=0o700)
            (source / "runtime.py").write_text("runtime\n")
            identity = self._identity()
            payload = (("runtime.py", hashlib.sha256(b"runtime\n").hexdigest()),)
            identity = ExpectedRuntimeIdentity(
                identity.source_commit,
                identity.tag_ref,
                identity.tag_object,
                identity.signature_sha256,
                hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest(),
                identity.vendor_manifest_sha256,
            )
            manifest = {
                "release": "v1.2.3",
                "source_commit": identity.source_commit,
                "tag_ref": identity.tag_ref,
                "tag_object": identity.tag_object,
                "signature_sha256": identity.signature_sha256,
                "trust_policy_sha256": identity.trust_policy_sha256,
                "vendor_manifest_sha256": identity.vendor_manifest_sha256,
            }
            policy = RuntimeTrustPolicy(identity, payload)
            staged = stage_runtime_release(source, root / "releases", "v1.2.3", manifest, policy)
            (staged / "runtime.py").chmod(0o644)
            with self.assertRaisesRegex(RuntimeStoreError, "private regular"):
                verify_runtime_release(staged, policy)
            (staged / "runtime.py").chmod(0o600)
            altered = ExpectedRuntimeIdentity(
                identity.source_commit,
                identity.tag_ref,
                identity.tag_object,
                identity.signature_sha256,
                "f" * 64,
                identity.vendor_manifest_sha256,
            )
            with self.assertRaisesRegex(RuntimeStoreError, "identity"):
                verify_runtime_release(staged, RuntimeTrustPolicy(altered, payload))

    def test_verify_rejects_missing_unsafe_and_malformed_releases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = self._identity()
            policy = RuntimeTrustPolicy(identity, ())
            with self.assertRaisesRegex(RuntimeStoreError, "not a directory"):
                verify_runtime_release(root / "missing", policy)
            unsafe = root / "v1.2.3"
            unsafe.mkdir(mode=0o700)
            (unsafe / "runtime-manifest.json").write_text("{}")
            (unsafe / "runtime-manifest.json").chmod(0o600)
            unsafe.chmod(0o755)
            with self.assertRaisesRegex(RuntimeStoreError, "ownership or mode"):
                verify_runtime_release(unsafe, policy)
            unsafe.chmod(0o700)
            with self.assertRaisesRegex(RuntimeStoreError, "manifest is invalid"):
                verify_runtime_release(unsafe, policy)

    def test_verify_rejects_inventory_symlink_and_policy_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir(mode=0o700)
            (source / "runtime.py").write_text("runtime\n")
            identity = self._identity()
            payload = (("runtime.py", hashlib.sha256(b"runtime\n").hexdigest()),)
            identity = ExpectedRuntimeIdentity(
                identity.source_commit,
                identity.tag_ref,
                identity.tag_object,
                identity.signature_sha256,
                hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest(),
                identity.vendor_manifest_sha256,
            )
            manifest = {
                "release": "v1.2.3",
                "source_commit": identity.source_commit,
                "tag_ref": identity.tag_ref,
                "tag_object": identity.tag_object,
                "signature_sha256": identity.signature_sha256,
                "trust_policy_sha256": identity.trust_policy_sha256,
                "vendor_manifest_sha256": identity.vendor_manifest_sha256,
            }
            policy = RuntimeTrustPolicy(identity, payload)
            staged = stage_runtime_release(source, root / "releases", "v1.2.3", manifest, policy)
            (staged / "runtime.py").write_text("changed\n")
            (staged / "runtime.py").chmod(0o600)
            with self.assertRaisesRegex(RuntimeStoreError, "inventory"):
                verify_runtime_release(staged, policy)
            (staged / "runtime.py").write_text("runtime\n")
            (staged / "runtime.py").chmod(0o600)
            with (
                patch.object(RuntimeTrustPolicy, "digest", return_value="f" * 64),
                self.assertRaisesRegex(RuntimeStoreError, "trust policy digest"),
            ):
                verify_runtime_release(staged, policy)
            (staged / "runtime.py").unlink()
            (staged / "runtime.py").symlink_to("/etc/passwd")
            with self.assertRaisesRegex(RuntimeStoreError, "symlink"):
                verify_runtime_release(staged, policy)

    def test_stage_rejects_source_symlink_file_ancestor_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = self._identity()
            policy = RuntimeTrustPolicy(identity, ())
            source = root / "source"
            source.mkdir(mode=0o700)
            (source / "runtime-manifest.json").write_text("already\n")
            (source / "runtime-manifest.json").chmod(0o600)
            with self.assertRaisesRegex(RuntimeStoreError, "must not provide"):
                stage_runtime_release(
                    source,
                    root / "releases",
                    "v1.2.3",
                    {"release": "v1.2.3"},
                    policy,
                )
            file_parent = root / "file-parent"
            file_parent.write_text("file\n")
            with self.assertRaisesRegex(RuntimeStoreError, "ancestor is unsafe"):
                stage_runtime_release(
                    file_parent / "source",
                    root / "releases-2",
                    "v1.2.3",
                    {"release": "v1.2.3"},
                    policy,
                )
            source_link = root / "source-link"
            source_link.symlink_to(root)
            with self.assertRaisesRegex(RuntimeStoreError, "symlink"):
                stage_runtime_release(
                    source_link,
                    root / "releases-3",
                    "v1.2.3",
                    {"release": "v1.2.3"},
                    policy,
                )


if __name__ == "__main__":
    unittest.main()
