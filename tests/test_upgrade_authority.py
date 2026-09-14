# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the separate versioned runtime selector contract."""

from __future__ import annotations

import importlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import upgrade_authority
from tools.upgrade_authority import (
    AuthorityError,
    commit_runtime_selector,
    read_runtime_selector,
)


class RuntimeSelectorTests(unittest.TestCase):
    def test_handoffctl_is_package_safe(self) -> None:
        module = importlib.import_module("tools.handoffctl")
        self.assertTrue(callable(module.backend_selection))

    def test_handoffctl_script_mode_keeps_fallback(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(  # noqa: S603
            [sys.executable, str(root / "tools" / "handoffctl.py"), "--help"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_atomic_selector_round_trip_and_strict_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime-selector.json"
            commit_runtime_selector(path, "release-new", "release-old")
            self.assertEqual("release-new", read_runtime_selector(path)["active_release"])
            path.write_text('{"active_release":"new"}\n')
            with self.assertRaises(AuthorityError):
                read_runtime_selector(path)

    def test_selector_rejects_symlink_and_empty_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "selector.json"
            with self.assertRaises(AuthorityError):
                commit_runtime_selector(path, "", "old")
            target = root / "target"
            target.write_text("{}\n")
            path.symlink_to(target)
            with self.assertRaises(AuthorityError):
                commit_runtime_selector(path, "new", "old")

    def test_selector_publication_requires_private_real_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public = root / "public"
            public.mkdir(mode=0o755)
            public.chmod(0o755)
            with self.assertRaisesRegex(AuthorityError, "owner-only provisioned"):
                commit_runtime_selector(public / "selector.json", "new", "old")
            self.assertFalse((public / "selector.json").exists())

            private = root / "private"
            private.mkdir(mode=0o700)
            linked = root / "linked"
            linked.symlink_to(private, target_is_directory=True)
            with self.assertRaisesRegex(AuthorityError, "parent descriptor is unsafe"):
                commit_runtime_selector(linked / "selector.json", "new", "old")
            self.assertFalse((private / "selector.json").exists())

    def test_selector_publication_detects_parent_replacement_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "runtime"
            parent.mkdir(mode=0o700)
            selector = parent / "selector.json"
            commit_runtime_selector(selector, "old", "older")
            displaced = root / "displaced"
            original_recheck = upgrade_authority._recheck_parent

            def replace_parent(path: Path, identity: tuple[int, int]) -> None:
                parent.rename(displaced)
                parent.mkdir(mode=0o700)
                original_recheck(path, identity)

            with (
                patch.object(upgrade_authority, "_recheck_parent", side_effect=replace_parent),
                self.assertRaisesRegex(AuthorityError, "publication failed"),
            ):
                commit_runtime_selector(selector, "new", "old")
            self.assertFalse(selector.exists())
            self.assertEqual(
                "old", read_runtime_selector(displaced / "selector.json")["active_release"]
            )
            self.assertFalse(
                any(path.name.startswith(".selector.json.") for path in displaced.iterdir())
            )


if __name__ == "__main__":
    unittest.main()
