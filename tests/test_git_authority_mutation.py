# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile tests for the isolated Git authority commit capability."""

# The executable and arguments are fixed test fixtures.
# ruff: noqa: S603, S607

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from tools.authority_neutral_commit import CommitAdmissionBundle
from tools.git_authority_mutation import (
    GitCommitCapability,
    GitMutationAmbiguousError,
    GitMutationError,
)


def _git(root: Path, *args: str) -> str:
    # Test-only fixed executable and arguments.
    result = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.rstrip("\n")


class GitCommitCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _git(self.root, "init", "-b", "main")
        _git(self.root, "config", "user.name", "Test Runner")
        _git(self.root, "config", "user.email", "test@example.invalid")
        (self.root / "state").write_text("old\n", encoding="utf-8")
        _git(self.root, "add", "state")
        _git(self.root, "commit", "-m", "initial")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _admission() -> CommitAdmissionBundle:
        return CommitAdmissionBundle(
            backend="git",
            target="new",
            operation_id="op-1:commit",
            fencing_token="fence-1",  # noqa: S106
            state_revision=1,
            barrier_id="barrier-1",
            artifact_identity="artifact-1",
            manifest_identity="manifest-1",
            selector_identity="selector-1",
            runtime_identity="runtime-1",
        )

    def _capability(self) -> GitCommitCapability:
        return GitCommitCapability(
            self.root,
            admission=self._admission(),
            expected_branch="main",
            expected_head=_git(self.root, "rev-parse", "HEAD"),
        )

    def test_commits_only_pre_staged_change_and_verifies_postconditions(self) -> None:
        (self.root / "state").write_text("new\n", encoding="utf-8")
        _git(self.root, "add", "state")
        result = self._capability().commit("op-1 authority commit")
        self.assertEqual("main", result.branch)
        self.assertNotEqual(result.before_head, result.after_head)
        self.assertTrue(result.mutates_authority)
        self.assertEqual("", _git(self.root, "status", "--porcelain=v1", "--untracked-files=all"))

    def test_capability_is_single_use_but_fresh_capability_reopens(self) -> None:
        (self.root / "state").write_text("first\n", encoding="utf-8")
        _git(self.root, "add", "state")
        capability = self._capability()
        capability.commit("op-1 authority commit")

        with self.assertRaisesRegex(GitMutationError, "already consumed"):
            capability.commit("op-1 replay")

        (self.root / "state").write_text("second\n", encoding="utf-8")
        _git(self.root, "add", "state")
        reopened_admission = CommitAdmissionBundle(
            backend="git",
            target="new",
            operation_id="op-2:commit",
            fencing_token="fence-2",  # noqa: S106
            state_revision=2,
            barrier_id="barrier-2",
            artifact_identity="artifact-1",
            manifest_identity="manifest-1",
            selector_identity="selector-1",
            runtime_identity="runtime-1",
        )
        result = GitCommitCapability(
            self.root,
            admission=reopened_admission,
            expected_branch="main",
            expected_head=_git(self.root, "rev-parse", "HEAD"),
        ).commit("op-2 authority commit")
        self.assertEqual("fence-2", result.fencing_token)
        self.assertEqual("second\n", (self.root / "state").read_text(encoding="utf-8"))

    def test_rejects_unstaged_or_empty_changes(self) -> None:
        with self.assertRaisesRegex(GitMutationError, "no staged"):
            self._capability().commit("op-1 authority commit")
        (self.root / "state").write_text("new\n", encoding="utf-8")
        with self.assertRaisesRegex(GitMutationError, "unstaged"):
            self._capability().commit("op-1 authority commit")

    def test_rejects_head_drift_and_ambiguous_runner_outcome(self) -> None:
        (self.root / "state").write_text("new\n", encoding="utf-8")
        _git(self.root, "add", "state")
        stale = GitCommitCapability(
            self.root,
            admission=self._admission(),
            expected_branch="main",
            expected_head="0" * 40,
        )
        with self.assertRaisesRegex(GitMutationError, "identity changed"):
            stale.commit("op-1 authority commit")

        calls = 0

        def timeout_runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal calls
            calls += 1
            if calls == 4:
                raise subprocess.TimeoutExpired("git", 30)
            return cast(
                subprocess.CompletedProcess[str],
                subprocess.run(cast(Any, _args[0]), **cast(Any, _kwargs)),
            )

        with self.assertRaises(GitMutationAmbiguousError):
            GitCommitCapability(
                self.root,
                admission=self._admission(),
                expected_branch="main",
                expected_head=self._capability()._expected_head,
                runner=timeout_runner,
            ).commit("op-1 authority commit")

    def test_rejects_a_concurrent_commit_after_the_effect(self) -> None:
        (self.root / "state").write_text("new\n", encoding="utf-8")
        _git(self.root, "add", "state")

        def racing_runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            command = cast(list[str], args[0])
            result = cast(
                subprocess.CompletedProcess[str],
                subprocess.run(command, **cast(Any, kwargs)),
            )
            if command[3:4] == ["commit"] and result.returncode == 0:
                (self.root / "race").write_text("concurrent\n", encoding="utf-8")
                _git(self.root, "add", "race")
                _git(self.root, "commit", "--no-verify", "-m", "concurrent writer")
            return result

        with self.assertRaisesRegex(GitMutationAmbiguousError, "postcondition"):
            GitCommitCapability(
                self.root,
                admission=self._admission(),
                expected_branch="main",
                expected_head=self._capability()._expected_head,
                runner=racing_runner,
            ).commit("op-1 authority commit")


if __name__ == "__main__":
    unittest.main()
