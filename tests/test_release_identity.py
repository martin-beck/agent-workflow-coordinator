# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Hostile tests for read-only release provenance verification."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from tools.verify_release_identity import (
    ReleaseIdentityError,
    _signature_digest,
    _transition_path,
    verify_transition,
)


def _release(version: str, commit: str, tag_object: str, signature: str) -> dict[str, str]:
    return {
        "version": version,
        "source_commit": commit,
        "tag_ref": f"refs/tags/{version}",
        "tag_object": tag_object,
        "signature_sha256": signature,
        "trust_policy_sha256": "a" * 64,
        "vendor_manifest_sha256": "b" * 64,
    }


class ReleaseIdentityTests(unittest.TestCase):
    @staticmethod
    def _run_git(root: Path, *args: str, text: bool = True) -> str | bytes:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), *args],  # noqa: S607
            check=True,
            capture_output=True,
            text=text,
        )
        stdout = cast(str | bytes, result.stdout)
        return stdout if not text else cast(str, stdout).strip()

    def _tag_fixture(self, root: Path, version: str, message: bytes) -> dict[str, str]:
        """Create a real SSH-signed annotated tag and derive identities from Git."""
        (root / "payload.txt").write_text(version, encoding="utf-8")
        self._run_git(root, "add", "payload.txt")
        self._run_git(root, "commit", "-m", f"source {version}")
        self._run_git(root, "tag", "-s", version, "-m", message.decode("ascii"))
        # This verifies the fixture's SSH signature cryptographically rather
        # than merely checking that its tag message contains marker strings.
        self._run_git(root, "verify-tag", version)
        tag_object = str(self._run_git(root, "rev-parse", f"refs/tags/{version}^{{tag}}"))
        contents = self._run_git(root, "cat-file", "tag", tag_object, text=False)
        assert isinstance(contents, bytes)
        begin = contents.find(b"-----BEGIN SSH SIGNATURE-----")
        end = contents.find(b"-----END SSH SIGNATURE-----", begin)
        assert begin >= 0 and end >= 0
        end += len(b"-----END SSH SIGNATURE-----")
        return {
            "version": version,
            "source_commit": cast(
                str, self._run_git(root, "rev-parse", f"refs/tags/{version}^{{commit}}")
            ),
            "tag_ref": f"refs/tags/{version}",
            "tag_object": tag_object,
            "signature_sha256": hashlib.sha256(contents[begin:end]).hexdigest(),
            "trust_policy_sha256": "a" * 64,
            "vendor_manifest_sha256": "b" * 64,
        }

    def _transition_fixture(self, root: Path) -> dict[str, Any]:
        self._run_git(root, "init", "--initial-branch=main")
        self._run_git(root, "config", "user.name", "Release Fixture")
        self._run_git(root, "config", "user.email", "fixture@example.invalid")
        key = root / "fixture-signing-key"
        self._run_external("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key))
        self._run_git(root, "config", "gpg.format", "ssh")
        self._run_git(root, "config", "user.signingkey", str(key))
        allowed = root / "allowed-signers"
        public_key = key.with_suffix(".pub").read_text(encoding="utf-8").strip()
        allowed.write_text(f"fixture@example.invalid {public_key}\n", encoding="utf-8")
        self._run_git(root, "config", "gpg.ssh.allowedSignersFile", str(allowed))
        block_old = b"-----BEGIN SSH SIGNATURE-----\nold\n-----END SSH SIGNATURE-----"
        block_new = b"-----BEGIN SSH SIGNATURE-----\nnew\n-----END SSH SIGNATURE-----"
        old = self._tag_fixture(root, "v0.3.7", block_old)
        new = self._tag_fixture(root, "v0.3.8", block_new)
        return {
            "operation_id": "upgrade:v0.3.7-to-v0.3.8:001",
            "backend": "sqlite",
            "selector_ref": ".runtime/runtime-selector.json",
            "expected_state_revision": 7,
            "barrier_id": "barrier-7",
            "fencing_token": "fence-7",
            "from": old,
            "to": new,
        }

    @staticmethod
    def _run_external(*args: str) -> str:
        result = subprocess.run(  # noqa: S603
            list(args),
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def test_signature_digest_binds_exact_supported_block(self) -> None:
        block = b"-----BEGIN SSH SIGNATURE-----\nabc\n-----END SSH SIGNATURE-----"
        self.assertEqual(hashlib.sha256(block).hexdigest(), _signature_digest(b"header\n" + block))

    def test_signature_digest_rejects_missing_or_truncated_block(self) -> None:
        with self.assertRaisesRegex(ReleaseIdentityError, "no supported SSH signature"):
            _signature_digest(b"unsigned tag")
        with self.assertRaisesRegex(ReleaseIdentityError, "no supported SSH signature"):
            _signature_digest(b"-----BEGIN SSH SIGNATURE-----\npartial")

    def test_transition_verifies_both_tag_objects_and_commits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transition = self._transition_fixture(Path(directory))
            self.assertEqual(
                {"status": "pass", "from": "v0.3.7", "to": "v0.3.8"},
                verify_transition(Path(directory), transition),
            )

    def test_transition_rejects_unsigned_marker_tag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transition = self._transition_fixture(root)
            self._run_git(root, "tag", "-d", "v0.3.8")
            fake = b"-----BEGIN SSH SIGNATURE-----\nforged\n-----END SSH SIGNATURE-----"
            self._run_git(root, "tag", "-a", "v0.3.8", "-m", fake.decode("ascii"))
            tag_object = str(self._run_git(root, "rev-parse", "refs/tags/v0.3.8^{tag}"))
            contents = self._run_git(root, "cat-file", "tag", tag_object, text=False)
            assert isinstance(contents, bytes)
            transition["to"] = {
                **transition["to"],
                "tag_object": tag_object,
                "signature_sha256": _signature_digest(contents),
            }
            with self.assertRaisesRegex(ReleaseIdentityError, "release Git inspection failed"):
                verify_transition(root, transition)

    def test_transition_rejects_forged_source_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transition = self._transition_fixture(Path(directory))
            transition["from"] = {**transition["from"], "source_commit": "a" * 40}
            with self.assertRaisesRegex(ReleaseIdentityError, "source commit"):
                verify_transition(Path(directory), transition)

    def test_transition_rejects_forged_tag_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transition = self._transition_fixture(Path(directory))
            transition["to"] = {**transition["to"], "tag_object": "a" * 40}
            with self.assertRaisesRegex(ReleaseIdentityError, "tag object"):
                verify_transition(Path(directory), transition)

    def test_transition_rejects_forged_signature(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transition = self._transition_fixture(Path(directory))
            transition["to"] = {**transition["to"], "signature_sha256": "a" * 64}
            with self.assertRaisesRegex(ReleaseIdentityError, "signature"):
                verify_transition(Path(directory), transition)

    def test_transition_path_rejects_outside_and_symlink_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            outside = Path(directory) / "outside.json"
            root.mkdir()
            outside.write_text("{}", encoding="utf-8")
            inside = root / "transition.json"
            inside.write_text("{}", encoding="utf-8")
            root.chmod(0o755)
            inside.chmod(0o644)
            self.assertEqual(inside, _transition_path(Path("transition.json"), root))
            with self.assertRaisesRegex(ReleaseIdentityError, "escapes workspace"):
                _transition_path(outside, root)
            (root / "alias.json").symlink_to(outside)
            with self.assertRaisesRegex(ReleaseIdentityError, "regular workspace file"):
                _transition_path(Path("alias.json"), root)
            (root / "linkdir").symlink_to(Path(directory))
            with self.assertRaisesRegex(ReleaseIdentityError, "regular workspace file"):
                _transition_path(Path("linkdir/outside.json"), root)
            inside.chmod(0o666)
            with self.assertRaisesRegex(ReleaseIdentityError, "owner-controlled"):
                _transition_path(Path("transition.json"), root)
            inside.chmod(0o644)
            nested = root / "nested"
            nested.mkdir()
            nested.chmod(0o777)
            nested_transition = nested / "transition.json"
            nested_transition.write_text("{}", encoding="utf-8")
            nested_transition.chmod(0o644)
            with self.assertRaisesRegex(ReleaseIdentityError, "parent must be owner-controlled"):
                _transition_path(Path("nested/transition.json"), root)


if __name__ == "__main__":
    unittest.main()
