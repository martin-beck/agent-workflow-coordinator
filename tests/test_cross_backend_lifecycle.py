# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Cross-backend backup/restore lifecycle correspondence checks."""

from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from tools.formal_correspondence import formal_provenance, validate_runtime_trace

ROOT = Path(__file__).resolve().parents[1]


def load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GIT = load("git_backup")
SQLITE = load("sqlite_backup")
BINDING = {
    "project_id": "11111111-1111-4111-8111-111111111111",
    "state_repository": "owner/state",
    "product_repository": "owner/product",
}


class CrossBackendLifecycleTests(unittest.TestCase):
    def test_verified_fresh_restore_and_publication_faults_preserve_both_authorities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)  # noqa: S607
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],  # noqa: S607
                cwd=repo,
                check=True,
            )
            subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)  # noqa: S607
            (repo / "state.txt").write_text("git-state\n")
            subprocess.run(["git", "add", "state.txt"], cwd=repo, check=True)  # noqa: S607
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)  # noqa: S607
            database = root / "authority.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("CREATE TABLE state(value TEXT NOT NULL)")
            connection.executemany(
                "INSERT INTO metadata VALUES (?, ?)",
                [
                    ("schema_version", "1"),
                    ("backend", "sqlite"),
                    *BINDING.items(),
                    ("state", "active"),
                ],
            )
            connection.execute("INSERT INTO state VALUES ('sqlite-state')")
            connection.commit()
            connection.close()

            git_backup = GIT.create_backup(repo, root / "git-backup", quiesced=True)
            sqlite_backup = root / "sqlite-backup.sqlite3"
            sqlite_manifest = SQLITE.backup_database(database, sqlite_backup, BINDING)
            trace: list[dict[str, object]] = []
            GIT.verify_backup(git_backup)
            SQLITE.verify_backup(sqlite_backup, sqlite_manifest, BINDING)
            trace.append({"event": "backup_verified", "revision_before": 0, "revision_after": 0})
            self.assertEqual(("ExecuteSuccess",), validate_runtime_trace(trace))
            provenance = formal_provenance(ROOT)
            self.assertEqual(64, len(cast(str, provenance["model_sha256"])))
            self.assertEqual(64, len(cast(str, provenance["artifact_sha256"])))

            with patch.object(Path, "replace", side_effect=OSError("publication interrupted")):
                with self.assertRaises(GIT.BackupError):
                    GIT.restore_backup(git_backup, root / "git-fresh")
                trace.append(
                    {
                        "event": "publication_failed",
                        "revision_before": 0,
                        "revision_after": 0,
                        "lock_held": True,
                    }
                )
                self.assertEqual(("ExecuteReject",), validate_runtime_trace(trace[-1:]))
                with self.assertRaises(SQLITE.BackupError):
                    SQLITE.restore_database(
                        sqlite_backup,
                        root / "sqlite-fresh.sqlite3",
                        sqlite_manifest,
                        BINDING,
                        quiesced=True,
                    )
            self.assertFalse((root / "git-fresh").exists())
            self.assertFalse((root / "sqlite-fresh.sqlite3").exists())
            self.assertFalse(list(root.glob(".git-restore-*")))
            self.assertFalse(list(root.glob(".coordinator-*")))

            GIT.restore_backup(git_backup, root / "git-fresh")
            SQLITE.restore_database(
                sqlite_backup,
                root / "sqlite-fresh.sqlite3",
                sqlite_manifest,
                BINDING,
                quiesced=True,
            )
            self.assertEqual("git-state\n", (root / "git-fresh" / "state.txt").read_text())
            with closing(sqlite3.connect(root / "sqlite-fresh.sqlite3")) as restored:
                self.assertEqual(
                    "sqlite-state", restored.execute("SELECT value FROM state").fetchone()[0]
                )
