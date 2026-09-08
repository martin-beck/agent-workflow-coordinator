# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Tests for deterministic offline coordinator vendoring."""

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "tools/vendor.py"
SPEC = importlib.util.spec_from_file_location("handoffctl_vendor", SOURCE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load vendor tool")
VENDOR: Any = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VENDOR)


class VendorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.target = Path(self.temporary.name) / "target"
        self.target.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_sync_verify_and_detect_tampering(self) -> None:
        commit = "a" * 40
        profile = self.target / ".handoffctl.json"
        binding = self.target / "coordinator.binding.json"
        profile.write_text("project-profile-sentinel\n")
        binding.write_text("project-binding-sentinel\n")
        with patch("builtins.print") as output:
            VENDOR.sync(ROOT, self.target, "v0.2.0", commit)
        output.assert_called_once()
        self.assertEqual("project-profile-sentinel\n", profile.read_text())
        self.assertEqual("project-binding-sentinel\n", binding.read_text())
        lock = json.loads((self.target / VENDOR.LOCK_NAME).read_text())
        self.assertEqual(commit, lock["upstream"]["commit"])
        self.assertEqual(
            {destination for _, destination in VENDOR.SOURCE_FILES}, set(lock["files"])
        )
        self.assertTrue(os.access(self.target / "tools/handoffctl", os.X_OK))
        with patch("builtins.print"):
            VENDOR.verify(self.target)
        (self.target / "tools/handoffctl.py").write_text("tampered\n")
        with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
            VENDOR.verify(self.target)

    def test_rejects_bad_release_identity_and_lock_shapes(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "invalid release"):
            VENDOR.build_lock(ROOT, "latest", "short")
        (self.target / VENDOR.LOCK_NAME).write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "lock structure"):
            VENDOR.verify(self.target)
        (self.target / VENDOR.LOCK_NAME).write_text("not-json")
        with self.assertRaisesRegex(RuntimeError, "cannot read"):
            VENDOR.verify(self.target)

    def test_release_identity_requires_clean_exact_tag(self) -> None:
        with patch.object(VENDOR, "git_output", side_effect=["", "b" * 40, "v0.2.0"]):
            self.assertEqual("b" * 40, VENDOR.release_identity(ROOT, "v0.2.0"))
        with (
            patch.object(VENDOR, "git_output", return_value="dirty"),
            self.assertRaisesRegex(RuntimeError, "must be clean"),
        ):
            VENDOR.release_identity(ROOT, "v0.2.0")
        with (
            patch.object(VENDOR, "git_output", side_effect=["", "b" * 40, "v0.3.0"]),
            self.assertRaisesRegex(RuntimeError, "not tagged"),
        ):
            VENDOR.release_identity(ROOT, "v0.2.0")
        with self.assertRaisesRegex(RuntimeError, "form vMAJOR"):
            VENDOR.release_identity(ROOT, "main")

    def test_verify_rejects_every_identity_and_manifest_boundary(self) -> None:
        commit = "d" * 40
        with patch("builtins.print"):
            VENDOR.sync(ROOT, self.target, "v0.2.0", commit)
        original = json.loads((self.target / VENDOR.LOCK_NAME).read_text())
        variants = []
        value = json.loads(json.dumps(original))
        value["schema_version"] = 2
        variants.append(value)
        value = json.loads(json.dumps(original))
        value["upstream"] = {}
        variants.append(value)
        value = json.loads(json.dumps(original))
        value["upstream"]["repository"] = "other"
        variants.append(value)
        value = json.loads(json.dumps(original))
        value["upstream"]["version"] = "latest"
        variants.append(value)
        value = json.loads(json.dumps(original))
        value["files"] = {}
        variants.append(value)
        value = json.loads(json.dumps(original))
        first = next(iter(value["files"]))
        value["files"][first] = {}
        variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                (self.target / VENDOR.LOCK_NAME).write_text(json.dumps(value))
                VENDOR.verify(self.target)
        (self.target / VENDOR.LOCK_NAME).write_text(json.dumps(original))
        core = self.target / "tools/handoffctl.py"
        core.write_text(
            core.read_text().replace(
                'COORDINATOR_VERSION = "0.2.0"', 'COORDINATOR_VERSION = "9.9.9"'
            )
        )
        original["files"]["tools/handoffctl.py"]["sha256"] = VENDOR.sha256(core)
        (self.target / VENDOR.LOCK_NAME).write_text(json.dumps(original))
        with self.assertRaisesRegex(RuntimeError, "runtime version"):
            VENDOR.verify(self.target)
        missing = self.target / "missing"
        with self.assertRaisesRegex(RuntimeError, "regular file"):
            VENDOR.sha256(missing)
        with (
            patch.object(VENDOR, "git_output", side_effect=["", "short", "v0.2.0"]),
            self.assertRaisesRegex(RuntimeError, "full commit"),
        ):
            VENDOR.release_identity(ROOT, "v0.2.0")

    def test_install_failure_rolls_back_every_destination(self) -> None:
        staged = self.target / "staged"
        destination_root = self.target / "installed"
        staged.mkdir()
        destination_root.mkdir()
        for name in ("one", "two"):
            (staged / name).write_text(f"new-{name}\n")
            (destination_root / name).write_text(f"old-{name}\n")
        original_replace = Path.replace

        def fail_second_install(path: Path, target: Path) -> Path:
            if path == staged / "two":
                raise OSError(5, "injected rename failure")
            return original_replace(path, target)

        with (
            patch.object(Path, "replace", fail_second_install),
            self.assertRaisesRegex(OSError, "injected rename"),
        ):
            VENDOR.install_staged_snapshot(staged, destination_root, ["one", "two"])
        self.assertEqual("old-one\n", (destination_root / "one").read_text())
        self.assertEqual("old-two\n", (destination_root / "two").read_text())

        (staged / "link").write_text("new\n")
        (destination_root / "link").symlink_to(destination_root / "one")
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            VENDOR.install_staged_snapshot(staged, destination_root, ["link"])

    def test_staging_failure_preserves_existing_vendor_snapshot(self) -> None:
        with patch("builtins.print"):
            VENDOR.sync(ROOT, self.target, "v0.2.0", "a" * 40)
        before = {
            destination: (self.target / destination).read_bytes()
            for _, destination in VENDOR.SOURCE_FILES
        }
        before[VENDOR.LOCK_NAME] = (self.target / VENDOR.LOCK_NAME).read_bytes()
        original = VENDOR.atomic_bytes
        calls = 0

        def fail_during_staging(path: Path, content: bytes, mode: int = 0o644) -> None:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError(28, "No space left on device")
            original(path, content, mode)

        with (
            patch.object(VENDOR, "atomic_bytes", side_effect=fail_during_staging),
            self.assertRaisesRegex(OSError, "No space left"),
        ):
            VENDOR.sync(ROOT, self.target, "v0.2.0", "b" * 40)
        after = {
            destination: (self.target / destination).read_bytes()
            for _, destination in VENDOR.SOURCE_FILES
        }
        after[VENDOR.LOCK_NAME] = (self.target / VENDOR.LOCK_NAME).read_bytes()
        self.assertEqual(before, after)

    def test_atomic_copy_rejects_symlink_and_git_query_is_bounded(self) -> None:
        source = self.target / "source"
        source.write_text("value")
        destination = self.target / "destination"
        destination.symlink_to(source)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            VENDOR.atomic_bytes(destination, b"replacement")
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "head\n", "")
            self.assertEqual("head", VENDOR.git_output(ROOT, "rev-parse", "HEAD"))
            self.assertEqual(30, run.call_args.kwargs["timeout"])

    def test_main_dispatches_sync_and_verify(self) -> None:
        with (
            patch.object(sys, "argv", ["vendor", "verify", "--target", str(self.target)]),
            patch.object(VENDOR, "verify") as verify,
        ):
            self.assertEqual(0, VENDOR.main())
            verify.assert_called_once_with(self.target)
        with (
            patch.object(
                sys,
                "argv",
                [
                    "vendor",
                    "sync",
                    "--source",
                    str(ROOT),
                    "--target",
                    str(self.target),
                    "--version",
                    "v0.2.0",
                ],
            ),
            patch.object(VENDOR, "release_identity", return_value="c" * 40),
            patch.object(VENDOR, "sync") as sync,
        ):
            self.assertEqual(0, VENDOR.main())
            sync.assert_called_once_with(ROOT, self.target, "v0.2.0", "c" * 40)


if __name__ == "__main__":
    unittest.main()
