# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Safety checks for the bounded Git command latency benchmark."""

import contextlib
import hashlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from git_command_latency_benchmark import (
    case_order,
    classify,
    digest_stream,
    domain_tree,
    history_depth,
    history_extends,
    non_head_refs_digest,
    overlay_digest,
    percentile,
    product_input_digest,
    run_samples,
    snapshot_body_digest,
)


class GitCommandLatencyBenchmarkTests(unittest.TestCase):
    def test_domain_tree_ignores_only_intentionally_different_vendor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)  # noqa: S603,S607
            (root / "tools").mkdir()
            (root / "tasks").mkdir()
            tool = root / "tools" / "handoffctl.py"
            task = root / "tasks" / "AR-0001.md"
            policy = root / "task-spec-policy.json"
            tool.write_text("old\n")
            task.write_text("old\n")
            policy.write_text("{}\n")
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)  # noqa: S603,S607
            subprocess.run(  # noqa: S603
                [
                    "/usr/bin/git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "fixture",
                ],
                check=True,
            )
            original = domain_tree(root)
            original_overlay = overlay_digest(root)
            tool.write_text("new\n")
            self.assertEqual(original, domain_tree(root))
            self.assertNotEqual(original_overlay, overlay_digest(root))
            unexpected_tool = root / "tools" / "unexpected.py"
            unexpected_tool.write_text("effect\n")
            self.assertNotEqual(original, domain_tree(root))
            unexpected_tool.unlink()
            runtime = root / ".runtime"
            runtime.mkdir()
            (runtime / "roles.json").write_text("effect\n")
            self.assertNotEqual(original, domain_tree(root))
            (runtime / "roles.json").unlink()
            task.write_text("dirty\n")
            self.assertNotEqual(original, domain_tree(root))
            task.write_text("old\n")
            policy.write_text('{"changed":true}\n')
            self.assertNotEqual(original, domain_tree(root))
            policy.write_text("{}\n")
            extra = root / "untracked.md"
            extra.write_text("new\n")
            self.assertNotEqual(original, domain_tree(root))
            extra.unlink()
            task.write_text("staged\n")
            subprocess.run(["git", "-C", str(root), "add", "tasks/AR-0001.md"], check=True)  # noqa: S603,S607
            task.write_text("old\n")
            self.assertNotEqual(original, domain_tree(root))

    def test_history_depth_detects_same_tree_extra_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)  # noqa: S603,S607
            path = root / "README.md"
            path.write_text("unchanged\n")
            subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)  # noqa: S603,S607
            identity = [
                "/usr/bin/git",
                "-C",
                str(root),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
            ]
            subprocess.run([*identity, "commit", "-q", "-m", "initial"], check=True)  # noqa: S603
            before_tree, before_depth = domain_tree(root), history_depth(root)
            before_refs = non_head_refs_digest(root)
            before_head = subprocess.run(  # noqa: S603
                ["/usr/bin/git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(  # noqa: S603
                [*identity, "commit", "-q", "--allow-empty", "-m", "extra"],
                check=True,
            )
            self.assertEqual(before_tree, domain_tree(root))
            self.assertEqual(before_depth + 1, history_depth(root))
            self.assertTrue(history_extends(root, before_head))
            self.assertEqual(before_refs, non_head_refs_digest(root))
            subprocess.run(  # noqa: S603
                ["/usr/bin/git", "-C", str(root), "tag", "unexpected-ref"],
                check=True,
            )
            self.assertNotEqual(before_refs, non_head_refs_digest(root))
            extra_head = subprocess.run(  # noqa: S603
                ["/usr/bin/git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(  # noqa: S603
                [*identity, "commit", "-q", "--amend", "--allow-empty", "-m", "rewritten"],
                check=True,
            )
            self.assertEqual(before_depth + 1, history_depth(root))
            self.assertFalse(history_extends(root, extra_head))

    def test_percentiles_use_bounded_nearest_rank(self) -> None:
        values = [9.0, 3.0, 1.0, 7.0, 5.0]
        self.assertEqual(5.0, percentile(values, 0.5))
        self.assertEqual(9.0, percentile(values, 0.95))
        self.assertEqual(9.0, percentile(values, 1))

    def test_cases_alternate_to_limit_warm_order_bias(self) -> None:
        self.assertEqual(("baseline", "candidate"), case_order(0))
        self.assertEqual(("candidate", "baseline"), case_order(1))

    def test_failures_are_not_counted_as_success(self) -> None:
        self.assertEqual("ok", classify(0, b""))
        self.assertEqual("lock_timeout", classify(1, b"LOCK_TIMEOUT"))
        self.assertEqual("stale_or_changed", classify(1, b"PROJECT_STATE.md is stale"))
        self.assertEqual("other_error", classify(2, b"failed to sign"))

    def test_output_digest_includes_all_bytes(self) -> None:
        payload = b"result\n" * 10000
        self.assertEqual(
            (hashlib.sha256(payload).hexdigest(), len(payload)), digest_stream(io.BytesIO(payload))
        )

    def test_snapshot_body_digest_only_ignores_valid_commit_prefix(self) -> None:
        body = b"# Current\nunchanged\n"
        first = io.BytesIO(b"STATE_COMMIT=" + b"a" * 40 + b"\n" + body)
        second = io.BytesIO(b"STATE_COMMIT=" + b"b" * 40 + b"\n" + body)
        self.assertEqual(
            snapshot_body_digest(first, "a" * 40), snapshot_body_digest(second, "b" * 40)
        )
        with self.assertRaises(ValueError):
            snapshot_body_digest(io.BytesIO(b"STATE_COMMIT=invalid\n" + body), "a" * 40)
        with self.assertRaises(ValueError):
            snapshot_body_digest(io.BytesIO(b"STATE_COMMIT=" + b"b" * 40 + b"\n" + body), "a" * 40)

    def test_product_fingerprint_detects_dirty_checkout_without_head_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)  # noqa: S603,S607
            source = root / "README.md"
            source.write_text("before\n")
            subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)  # noqa: S603,S607
            subprocess.run(  # noqa: S603
                [
                    "/usr/bin/git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "fixture",
                ],
                check=True,
            )
            before = product_input_digest(root)
            source.write_text("after\n")
            dirty = product_input_digest(root)
            self.assertNotEqual(before, dirty)
            source.write_text("later\n")
            self.assertNotEqual(dirty, product_input_digest(root))

    def test_product_fingerprint_detects_staged_changes_with_same_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)  # noqa: S603,S607
            source = root / "README.md"
            source.write_text("base\n")
            subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)  # noqa: S603,S607
            subprocess.run(  # noqa: S603
                [
                    "/usr/bin/git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "fixture",
                ],
                check=True,
            )
            source.write_text("staged-one\n")
            subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)  # noqa: S603,S607
            source.write_text("working\n")
            first = product_input_digest(root)
            source.write_text("staged-two\n")
            subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)  # noqa: S603,S607
            source.write_text("working\n")
            self.assertNotEqual(first, product_input_digest(root))

    def test_paired_samples_reject_stale_candidate(self) -> None:
        common = {
            "stdout_sha256": ["same"],
            "snapshot_body_sha256": None,
            "stdout_bytes": [4],
            "state_tree": "tree",
            "history_depth": 3,
            "history_extends": True,
            "overlay_unchanged": True,
            "non_head_refs_unchanged": True,
            "current_sha256": "view",
        }
        with (
            mock.patch(
                "git_command_latency_benchmark.measure",
                side_effect=[
                    {**common, "outcomes": {"ok": 1}},
                    {**common, "outcomes": {"stale_or_changed": 1}},
                ],
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertFalse(
                run_samples(
                    {"baseline": (Path("baseline"), {}), "candidate": (Path("candidate"), {})},
                    ("doctor",),
                    (1,),
                    1,
                    1,
                )
            )


if __name__ == "__main__":
    unittest.main()
