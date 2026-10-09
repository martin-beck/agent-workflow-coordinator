# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Fail-closed checks for the disposable, pinned worktree fixture builder."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from build_frozen_git_fixture import assert_no_source_overlap, parse_worktrees


class FrozenGitFixtureTests(unittest.TestCase):
    def test_output_must_not_overlap_any_source_checkout(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sources = (root / "state", root / "product", root / "linked-worker")
            for source in sources:
                source.mkdir()
            assert_no_source_overlap(root / "fixture", sources)
            for bad_output in (root, root / "state" / "fixture", root / "linked-worker"):
                with self.subTest(output=bad_output), self.assertRaises(ValueError):
                    assert_no_source_overlap(bad_output, sources)

    def test_parse_pins_branches_and_detached_heads(self) -> None:
        listing = (
            "worktree /fixture/product\n"
            "HEAD " + "a" * 40 + "\n"
            "branch refs/heads/main\n\n"
            "worktree /fixture/worker\n"
            "HEAD " + "b" * 40 + "\n"
            "detached\n"
        )
        records = parse_worktrees(listing)
        self.assertEqual(
            [(Path("/fixture/product"), "main"), (Path("/fixture/worker"), None)],
            [(record.path, record.branch) for record in records],
        )
        self.assertEqual(["a" * 40, "b" * 40], [record.head for record in records])

    def test_rejects_prunable_or_incomplete_inventory(self) -> None:
        for listing in (
            "",
            "worktree /fixture/worker\nHEAD " + "a" * 40 + "\nprunable gitdir missing\n",
            "worktree relative\nHEAD " + "a" * 40 + "\n",
            "worktree /fixture/worker\nHEAD invalid\n",
            "worktree /fixture/worker\nHEAD " + "a" * 40 + "\nbranch refs/tags/v1\n",
        ):
            with self.subTest(listing=listing), self.assertRaises(ValueError):
                parse_worktrees(listing)


if __name__ == "__main__":
    unittest.main()
