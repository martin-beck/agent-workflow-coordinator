# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile tests for stable runtime selector resolution."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.runtime_bootstrap import resolve_selected_runtime
from tools.upgrade_authority import AuthorityError, commit_runtime_selector


class RuntimeBootstrapTests(unittest.TestCase):
    def test_resolves_owner_only_versioned_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            releases.chmod(0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            selected.chmod(0o700)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            self.assertEqual(selected, resolve_selected_runtime(selector, releases))

    def test_rejects_missing_symlink_and_unbounded_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            releases.chmod(0o700)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with self.assertRaisesRegex(AuthorityError, "unavailable"):
                resolve_selected_runtime(selector, releases)
            (releases / "v1.2.3").symlink_to(root)
            with self.assertRaisesRegex(AuthorityError, "unsafe"):
                resolve_selected_runtime(selector, releases)
            selector.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "active_release": "v1.2.3-extra",
                        "previous_release": "v1.2.2",
                    }
                )
            )
            with self.assertRaisesRegex(AuthorityError, "identity"):
                resolve_selected_runtime(selector, releases)
