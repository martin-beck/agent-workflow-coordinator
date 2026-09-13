# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile-path tests for read-only authority probes."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.authority_probe import ProbeError, probe_git, probe_sqlite

BINDING = {
    "project_id": "11111111-1111-4111-8111-111111111111",
    "state_repository": "owner/state",
    "product_repository": "owner/product",
}


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # noqa: S603,S607


class AuthorityProbeTests(unittest.TestCase):
    def test_git_probe_rejects_dirty_and_invalid_selector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "test@example.invalid")
            git(repo, "config", "user.name", "test")
            (repo / "state").write_text("ok\n")
            git(repo, "add", "state")
            git(repo, "commit", "-qm", "initial")
            selector = root / "selector.json"
            selector.write_text(json.dumps({"target": "new"}))
            self.assertTrue(probe_git(repo, selector)["state_clean"])
            selector.write_text("{}\n")
            with self.assertRaises(ProbeError):
                probe_git(repo, selector)
            (repo / "dirty").write_text("x\n")
            selector.write_text(json.dumps({"target": "new"}))
            with self.assertRaises(ProbeError):
                probe_git(repo, selector)

    def test_sqlite_probe_rejects_binding_and_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                )
                connection.executemany(
                    "INSERT INTO metadata VALUES (?, ?)",
                    [("backend", "sqlite"), *BINDING.items(), ("state", "active")],
                )
            selector = root / "selector.json"
            selector.write_text(json.dumps({"target": "rollback"}))
            self.assertTrue(probe_sqlite(database, selector, BINDING)["integrity_verified"])
            with self.assertRaises(ProbeError):
                probe_sqlite(database, selector, {**BINDING, "state_repository": "wrong"})


if __name__ == "__main__":
    unittest.main()
