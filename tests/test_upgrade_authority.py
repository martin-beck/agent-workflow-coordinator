# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the separate versioned runtime selector contract."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import upgrade_authority
from tools.upgrade_authority import (
    AuthorityError,
    SelectorPublicationAmbiguousError,
    commit_runtime_selector,
    read_runtime_selector,
    reconcile_runtime_selector,
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

    def test_inspect_authority_classifies_git_and_sqlite_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = MagicMock()
            fake.ROOT = root
            fake.project_binding.return_value = {
                "project_id": "11111111-1111-4111-8111-111111111111",
                "state_repository": "owner/state",
                "product_repository": "owner/product",
            }
            fake._assert_storage_binding.return_value = None
            fake.backend_selection.return_value = {"backend": "git", "legacy": False}
            with (
                patch.object(upgrade_authority, "_handoffctl", return_value=fake),
                patch("tools.upgrade_authority.subprocess.run") as run,
            ):
                run.return_value.stdout = ""
                result = upgrade_authority.inspect_authority()
                self.assertTrue(result["state_clean"])
                self.assertEqual("git", result["backend"])
            fake.backend_selection.return_value = {"backend": "sqlite", "legacy": True}
            fake.storage_backend.return_value.load_tasks.return_value = [1, 2]
            with patch.object(upgrade_authority, "_handoffctl", return_value=fake):
                result = upgrade_authority.inspect_authority()
            self.assertTrue(result["state_clean"])
            self.assertEqual(2, result["task_count"])

    def test_inspect_authority_rejects_dirty_git_and_failed_sqlite(self) -> None:
        fake = MagicMock()
        fake.ROOT = Path()
        fake.project_binding.return_value = {
            "project_id": "11111111-1111-4111-8111-111111111111",
            "state_repository": "owner/state",
            "product_repository": "owner/product",
        }
        fake.backend_selection.return_value = {"backend": "git", "legacy": False}
        with (
            patch.object(upgrade_authority, "_handoffctl", return_value=fake),
            patch("tools.upgrade_authority.subprocess.run") as run,
        ):
            run.return_value.stdout = " M tasks/AR-0001.md\n"
            with self.assertRaisesRegex(AuthorityError, "Git authority is dirty"):
                upgrade_authority.inspect_authority()
        fake.backend_selection.return_value = {"backend": "sqlite", "legacy": False}
        fake.storage_backend.side_effect = RuntimeError("database unavailable")
        with (
            patch.object(upgrade_authority, "_handoffctl", return_value=fake),
            self.assertRaisesRegex(AuthorityError, "SQLite authority inspection failed"),
        ):
            upgrade_authority.inspect_authority()

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

    def test_selector_rejects_control_characters_and_unbounded_release_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "selector.json"
            for release in ("with space", "with\nnewline", "with/slash", "x" * 129, 1):
                with self.subTest(release=release), self.assertRaises(AuthorityError):
                    commit_runtime_selector(path, release, "old")  # type: ignore[arg-type]
            path.write_text(
                '{"schema_version":1,"active_release":"bad\\nvalue","previous_release":"old"}\n'
            )
            with self.assertRaisesRegex(AuthorityError, "selector identity"):
                read_runtime_selector(path)

    def test_postrename_fsync_failure_requires_exact_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "selector.json"
            commit_runtime_selector(path, "old", "older")
            real_fsync = os.fsync
            calls = 0

            def fail_directory_fsync(descriptor: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("directory fsync failed")
                real_fsync(descriptor)

            with (
                patch("tools.upgrade_authority.os.fsync", side_effect=fail_directory_fsync),
                self.assertRaisesRegex(SelectorPublicationAmbiguousError, "reconcile"),
            ):
                commit_runtime_selector(path, "new", "old")
            handoffctl = MagicMock()
            with patch.object(upgrade_authority, "_handoffctl", return_value=handoffctl):
                self.assertEqual(
                    "committed",
                    reconcile_runtime_selector(
                        path,
                        before_active_release="old",
                        before_previous_release="older",
                        after_active_release="new",
                        after_previous_release="old",
                    ),
                )
            handoffctl.locked.assert_called_once_with()
            handoffctl.locked.return_value.__enter__.assert_called_once_with()
            commit_runtime_selector(path, "old", "older")
            self.assertEqual(
                "not-committed",
                reconcile_runtime_selector(
                    path,
                    before_active_release="old",
                    before_previous_release="older",
                    after_active_release="new",
                    after_previous_release="old",
                ),
            )
            commit_runtime_selector(path, "unexpected", "pair")
            with self.assertRaisesRegex(AuthorityError, "unknown release identity"):
                reconcile_runtime_selector(
                    path,
                    before_active_release="old",
                    before_previous_release="older",
                    after_active_release="new",
                    after_previous_release="old",
                )
            with self.assertRaisesRegex(AuthorityError, "identities are invalid"):
                reconcile_runtime_selector(
                    path,
                    before_active_release="same",
                    before_previous_release="pair",
                    after_active_release="same",
                    after_previous_release="pair",
                )

    def test_ambiguous_selector_cleanup_failure_preserves_classification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "selector.json"
            commit_runtime_selector(path, "old", "older")
            real_fsync = os.fsync
            calls = 0

            def fail_directory_fsync(descriptor: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("directory fsync failed")
                real_fsync(descriptor)

            with (
                patch("tools.upgrade_authority.os.fsync", side_effect=fail_directory_fsync),
                patch("tools.upgrade_authority.os.unlink", side_effect=OSError("cleanup failed")),
                self.assertRaisesRegex(
                    SelectorPublicationAmbiguousError, "temporary cleanup failed"
                ),
            ):
                commit_runtime_selector(path, "new", "old")
            self.assertEqual("new", read_runtime_selector(path)["active_release"])

    def test_selector_publication_requires_private_real_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public = root / "public"
            public.mkdir(mode=0o755)
            public.chmod(0o755)
            with self.assertRaisesRegex(AuthorityError, "owner-only provisioned"):
                commit_runtime_selector(public / "selector.json", "new", "old")
            self.assertFalse((public / "selector.json").exists())
            (public / "selector.json").write_text(
                '{"schema_version":1,"active_release":"new","previous_release":"old"}\n'
            )
            with self.assertRaisesRegex(AuthorityError, "owner-only provisioned"):
                read_runtime_selector(public / "selector.json")

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
