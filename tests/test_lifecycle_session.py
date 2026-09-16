# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Tests for adapter-owned lifecycle session identity."""

import tempfile
import unittest
from pathlib import Path

from tools.git_authority_adapter import GitAuthorityAdapter
from tools.lifecycle_session import LifecycleSession
from tools.sqlite_authority_adapter import SQLiteAuthorityAdapter


class LifecycleSessionTests(unittest.TestCase):
    def test_sessions_are_bound_to_concrete_adapters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            git_adapter = GitAuthorityAdapter(repo)
            git_session = git_adapter.lifecycle_session()
            self.assertTrue(git_session.belongs_to(git_adapter))
            self.assertFalse(git_session.belongs_to(GitAuthorityAdapter(repo)))

            authority = root / "authority.sqlite3"
            authority.touch(mode=0o600)
            sqlite_adapter = SQLiteAuthorityAdapter(authority)
            sqlite_session = sqlite_adapter.lifecycle_session()
            self.assertTrue(sqlite_session.belongs_to(sqlite_adapter))
            self.assertFalse(sqlite_session.belongs_to(git_adapter))

    def test_direct_construction_and_foreign_token_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            LifecycleSession(object(), (1, 2), b"caller supplied")
