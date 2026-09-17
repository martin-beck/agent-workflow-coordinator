# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the separate versioned runtime selector contract."""

# Test setup invokes fixed Git commands against isolated temporary repositories.
# ruff: noqa: S603, S607

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from tools import upgrade_authority
from tools.admission_lease import AdmissionLease, validate_recheck
from tools.upgrade_authority import (
    AuthorityError,
    SelectorPublicationAmbiguousError,
    commit_runtime_selector,
    commit_runtime_selector_admitted,
    read_git_authority_snapshot,
    read_runtime_selector,
    reconcile_runtime_selector,
)


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".git").chmod(0o700)
    (root / "state").write_text("clean\n")
    subprocess.run(["git", "-C", str(root), "add", "state"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example",
            "commit",
            "-qm",
            "init",
        ],
        check=True,
    )


class RuntimeSelectorTests(unittest.TestCase):
    def test_git_snapshot_binds_clean_head_and_requested_ref(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".git").chmod(0o700)
            (root / "state").write_text("clean\n")
            subprocess.run(["git", "-C", str(root), "add", "state"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example",
                    "commit",
                    "-qm",
                    "init",
                ],
                check=True,
            )
            snapshot = read_git_authority_snapshot(root, "HEAD")
            self.assertTrue(snapshot.clean)
            self.assertEqual(snapshot.head, snapshot.requested_ref_head)
            self.assertEqual("master", snapshot.branch)
            subprocess.run(["git", "-C", str(root), "tag", "v1"], check=True)
            (root / "state").write_text("second\n")
            subprocess.run(["git", "-C", str(root), "add", "state"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example",
                    "commit",
                    "-qm",
                    "second",
                ],
                check=True,
            )
            tagged = read_git_authority_snapshot(root, "refs/tags/v1")
            self.assertNotEqual(tagged.head, tagged.requested_ref_head)
            self.assertEqual(snapshot.head, tagged.requested_ref_head)

    def test_git_snapshot_rejects_dirty_detached_or_unreachable_ref(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".git").chmod(0o700)
            (root / "state").write_text("clean\n")
            subprocess.run(["git", "-C", str(root), "add", "state"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example",
                    "commit",
                    "-qm",
                    "init",
                ],
                check=True,
            )
            (root / "state").write_text("dirty\n")
            with self.assertRaisesRegex(AuthorityError, "not clean"):
                read_git_authority_snapshot(root)
            (root / "state").write_text("clean\n")
            subprocess.run(
                ["git", "-C", str(root), "checkout", "-q", "--detach", "HEAD"], check=True
            )
            with self.assertRaisesRegex(AuthorityError, "inspection was rejected"):
                read_git_authority_snapshot(root)
            with self.assertRaisesRegex(AuthorityError, "ref is invalid"):
                read_git_authority_snapshot(root, "--upload-pack=evil")

    def test_git_snapshot_rejects_missing_or_unsafe_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(AuthorityError, "repository is unavailable"):
                read_git_authority_snapshot(root)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".git").chmod(0o777)
            with self.assertRaisesRegex(AuthorityError, "owner-safe"):
                read_git_authority_snapshot(root)

    def test_git_snapshot_rejects_unreachable_requested_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".git").chmod(0o700)
            (root / "state").write_text("clean\n")
            subprocess.run(["git", "-C", str(root), "add", "state"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example",
                    "commit",
                    "-qm",
                    "init",
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "checkout", "-q", "--orphan", "other"], check=True
            )
            subprocess.run(["git", "-C", str(root), "rm", "-q", "-rf", "."], check=True)
            (root / "state").write_text("other\n")
            subprocess.run(["git", "-C", str(root), "add", "state"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example",
                    "commit",
                    "-qm",
                    "other",
                ],
                check=True,
            )
            with self.assertRaisesRegex(AuthorityError, "not reachable"):
                read_git_authority_snapshot(root, "refs/heads/master")

    def test_git_snapshot_normalizes_observation_and_reachability_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_git_repo(root)
            with (
                patch("tools.upgrade_authority.subprocess.run", side_effect=OSError("git")),
                self.assertRaisesRegex(AuthorityError, "inspection failed"),
            ):
                read_git_authority_snapshot(root)

            real_run = subprocess.run

            def reject_reachability(*args: Any, **kwargs: Any) -> Any:
                if "merge-base" in args[0]:
                    return subprocess.CompletedProcess(args[0], 1, "", "unreachable")
                return real_run(*args, **kwargs)

            with (
                patch("tools.upgrade_authority.subprocess.run", side_effect=reject_reachability),
                self.assertRaisesRegex(AuthorityError, "not reachable"),
            ):
                read_git_authority_snapshot(root)

            with (
                patch("tools.upgrade_authority.os.geteuid", return_value=os.geteuid() + 1),
                self.assertRaisesRegex(AuthorityError, "owner-safe"),
            ):
                read_git_authority_snapshot(root)

            def fail_check(*args: Any, **kwargs: Any) -> Any:
                if "merge-base" in args[0]:
                    raise OSError("git unavailable")
                return real_run(*args, **kwargs)

            with (
                patch("tools.upgrade_authority.subprocess.run", side_effect=fail_check),
                self.assertRaisesRegex(AuthorityError, "inspection failed"),
            ):
                read_git_authority_snapshot(root)

    def test_git_snapshot_rejects_identity_and_observation_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_git_repo(root)
            real_stat = Path.stat
            real_identity = upgrade_authority._file_identity
            initial_identity_seen = False

            def fail_reread(*args: Any, **_kwargs: Any) -> os.stat_result:
                if initial_identity_seen:
                    raise OSError("replaced")
                return real_stat(args[0] if args else root)

            def mark_initial(status: os.stat_result) -> tuple[int, int]:
                nonlocal initial_identity_seen
                initial_identity_seen = True
                return real_identity(status)

            with (
                patch.object(Path, "stat", side_effect=fail_reread),
                patch("tools.upgrade_authority._file_identity", side_effect=mark_initial),
                self.assertRaisesRegex(AuthorityError, "identity reread failed"),
            ):
                read_git_authority_snapshot(root)

            with (
                patch(
                    "tools.upgrade_authority._file_identity",
                    side_effect=[(1, 1), (2, 2), (1, 1), (3, 3)],
                ),
                self.assertRaisesRegex(AuthorityError, "identity changed"),
            ):
                read_git_authority_snapshot(root)

            real_run = subprocess.run
            head_calls = 0

            def drift_head(*args: Any, **kwargs: Any) -> Any:
                nonlocal head_calls
                if "HEAD^{commit}" in args[0]:
                    head_calls += 1
                    if head_calls == 2:
                        return subprocess.CompletedProcess(args[0], 0, "0" * 40 + "\n", "")
                return real_run(*args, **kwargs)

            with (
                patch("tools.upgrade_authority.subprocess.run", side_effect=drift_head),
                self.assertRaisesRegex(AuthorityError, "observation changed"),
            ):
                read_git_authority_snapshot(root)

    def test_admitted_selector_publication_requires_typed_ordered_lease(self) -> None:
        class Lease:
            def __init__(self, *, ordered: bool = True) -> None:
                self.events: list[str] = []
                self.ordered = ordered

            @contextmanager
            def hold(self) -> Any:
                self.events.append("hold")
                yield None
                self.events.append("release")

            def assert_ordered(self) -> None:
                self.events.append("assert-order")
                if not self.ordered or self.events != ["assert-order"]:
                    raise AuthorityError("selector admission lock order is invalid")

        lease = AdmissionLease(
            project_id="project",
            authority_revision="authority-1",
            fencing_token="fence-1",  # noqa: S106
            fencing_owner="owner-1",
            durable_barrier_id="barrier-1",
            revision=1,
        )
        recheck = validate_recheck(
            lease,
            project_id="project",
            authority_revision="authority-1",
            fencing_token="fence-1",  # noqa: S106
            fencing_owner="owner-1",
            durable_barrier_id="barrier-1",
            revision=1,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "selector.json"
            with self.assertRaisesRegex(AuthorityError, "lease is required"):
                commit_runtime_selector_admitted(path, "new", "old", None, recheck, Lease())  # type: ignore[arg-type]
            with self.assertRaisesRegex(AuthorityError, "lease is required"):
                commit_runtime_selector_admitted(path, "new", "old", object(), recheck, Lease())  # type: ignore[arg-type]

            unordered = Lease(ordered=False)
            with self.assertRaisesRegex(AuthorityError, "lock order is invalid"):
                commit_runtime_selector_admitted(path, "new", "old", lease, recheck, unordered)
            self.assertFalse(path.exists())

            other_lease = AdmissionLease(
                project_id="project",
                authority_revision="authority-1",
                fencing_token="fence-2",  # noqa: S106
                fencing_owner="owner-1",
                durable_barrier_id="barrier-2",
                revision=2,
            )
            other_recheck = validate_recheck(
                other_lease,
                project_id="project",
                authority_revision="authority-1",
                fencing_token="fence-2",  # noqa: S106
                fencing_owner="owner-1",
                durable_barrier_id="barrier-2",
                revision=2,
            )
            valid = Lease()
            with self.assertRaisesRegex(AuthorityError, "does not match"):
                commit_runtime_selector_admitted(path, "new", "old", lease, other_recheck, valid)
            self.assertFalse(path.exists())

            commit_runtime_selector_admitted(path, "new", "old", lease, recheck, valid)
            self.assertEqual(
                ["assert-order", "hold", "release"],
                valid.events,
            )
            self.assertEqual("new", read_runtime_selector(path)["active_release"])

    def test_handoffctl_is_package_safe(self) -> None:
        module = importlib.import_module("tools.handoffctl")
        self.assertTrue(callable(module.backend_selection))

    def test_handoffctl_script_mode_keeps_fallback(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
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
            self.assertEqual(
                {"selector.json"},
                {entry.name for entry in path.parent.iterdir()},
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
