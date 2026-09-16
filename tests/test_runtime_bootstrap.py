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
            self.assertEqual(selected, resolve_selected_runtime(selector, releases, lambda _: True))

    def test_rejects_missing_symlink_and_unbounded_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            releases.chmod(0o700)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with self.assertRaisesRegex(AuthorityError, "unavailable"):
                resolve_selected_runtime(selector, releases, lambda _: True)
            (releases / "v1.2.3").symlink_to(root)
            with self.assertRaisesRegex(AuthorityError, "unsafe"):
                resolve_selected_runtime(selector, releases, lambda _: True)
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
                resolve_selected_runtime(selector, releases, lambda _: True)

    def test_requires_authenticity_verifier_and_rejects_failed_verification(self) -> None:
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
            with self.assertRaisesRegex(AuthorityError, "verifier is required"):
                resolve_selected_runtime(selector, releases)
            with self.assertRaisesRegex(AuthorityError, "verification failed"):
                resolve_selected_runtime(selector, releases, lambda _: False)

    def test_rejects_symlinked_release_root_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real"
            real.mkdir(mode=0o700)
            (real / "v1.2.3").mkdir(mode=0o700)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(AuthorityError, "contains a symlink"):
                resolve_selected_runtime(selector, alias, lambda _: True)
