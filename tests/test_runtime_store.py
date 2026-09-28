"""Tests for offline versioned-runtime staging."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

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
        with tempfile.TemporaryDirectory(dir="/srv/data/projects") as directory:
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
        with tempfile.TemporaryDirectory(dir="/srv/data/projects") as directory:
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
        with tempfile.TemporaryDirectory(dir="/srv/data/projects") as directory:
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


if __name__ == "__main__":
    unittest.main()
