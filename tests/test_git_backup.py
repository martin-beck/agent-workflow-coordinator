# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Fault-path tests for complete Git backup artifacts."""

from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("git_backup", ROOT / "tools/git_backup.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
BackupError = MODULE.BackupError
create_backup = MODULE.create_backup
restore_backup = MODULE.restore_backup
verify_backup = MODULE.verify_backup


def git(*args: str, cwd: Path) -> None:
    subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
    )


class GitBackupTests(unittest.TestCase):
    def repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        git("init", "-q", cwd=repo)
        git("config", "user.email", "test@example.invalid", cwd=repo)
        git("config", "user.name", "test", cwd=repo)
        (repo / "task.md").write_text("state\n")
        git("add", "task.md", cwd=repo)
        git("commit", "-qm", "initial", cwd=repo)
        return repo

    def test_create_verify_and_restore_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self.repo(root)
            backup = create_backup(repo, root / "backup")
            self.assertEqual(True, verify_backup(backup)["verified"])
            restore_backup(backup, root / "restored")
            self.assertTrue((root / "restored" / "task.md").exists())

    def test_corrupt_artifact_and_nonempty_restore_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = create_backup(self.repo(root), root / "backup")
            valid_backup = create_backup(root / "repo", root / "valid-backup")
            (backup / "refs.txt").write_text("corrupt\n")
            with self.assertRaises(BackupError):
                verify_backup(backup)
            destination = root / "destination"
            destination.mkdir()
            (destination / "existing").write_text("preserve\n")
            with self.assertRaises(BackupError):
                restore_backup(valid_backup, destination)

    def test_backup_destination_and_manifest_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self.repo(root)
            occupied = root / "occupied"
            occupied.mkdir()
            (occupied / "sentinel").write_text("preserve\n")
            with self.assertRaises(BackupError):
                create_backup(repo, occupied)
            backup = create_backup(repo, root / "backup")
            (backup / "manifest.json").write_text("{}\n")
            with self.assertRaises(BackupError):
                verify_backup(backup)


if __name__ == "__main__":
    unittest.main()
