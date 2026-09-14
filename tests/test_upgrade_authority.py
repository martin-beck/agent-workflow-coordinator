# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the separate versioned runtime selector contract."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.upgrade_authority import (
    AuthorityError,
    commit_runtime_selector,
    read_runtime_selector,
)


class RuntimeSelectorTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
