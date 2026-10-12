# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Contract coverage for bounded, revision-fenced change-drift records."""

from __future__ import annotations

import argparse
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools import change_drift


def git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603
        ["/usr/bin/git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
    )


class ChangeDriftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        git(self.root, "init")
        git(self.root, "config", "user.email", "test@example.invalid")
        git(self.root, "config", "user.name", "Test")
        (self.root / "src").mkdir()
        (self.root / "src" / "old.py").write_text("old\n", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "initial")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_baseline_delta_preserves_rename_and_is_deterministic(self) -> None:
        first = change_drift.baseline("AR-0001", 2, "2026-10-12T00:00:00+00:00", self.root, ["src"])
        second = change_drift.baseline("AR-0001", 2, "different", self.root, ["src"])
        self.assertEqual(first["digest"], second["digest"])
        git(self.root, "mv", "src/old.py", "src/new.py")
        record = change_drift.delta("AR-0001", 3, "now", self.root, first, ["src"])
        entry = record["payload"]["entries"][0]
        self.assertEqual("src/new.py", entry["path"])
        self.assertEqual("src/old.py", entry["previous_path"])
        self.assertEqual("in_scope", entry["classification"])

    def test_generated_and_outside_paths_need_explicit_explanations(self) -> None:
        baseline = change_drift.baseline("AR-0001", 2, "now", self.root, ["src"])
        (self.root / "build").mkdir()
        (self.root / "build" / "bundle.generated").write_text("x", encoding="utf-8")
        current = change_drift.delta("AR-0001", 3, "now", self.root, baseline, ["src"])
        entry = current["payload"]["entries"][0]
        self.assertTrue(entry["generated"])
        history = [baseline, current]
        self.assertEqual([entry], change_drift.unresolved(history))
        explanation = change_drift.explain(
            "AR-0001", 4, "now", current["digest"], ["build/bundle.generated"], "build output", True
        )
        self.assertEqual([], change_drift.unresolved([*history, explanation]))

    def test_delta_detects_committed_work_and_dirty_content_changed_since_baseline(self) -> None:
        baseline = change_drift.baseline("AR-0001", 2, "now", self.root, ["src"])
        (self.root / "src" / "old.py").write_text("committed\n", encoding="utf-8")
        git(self.root, "add", "src/old.py")
        git(self.root, "commit", "-m", "committed work")
        committed = change_drift.delta("AR-0001", 3, "now", self.root, baseline, ["src"])
        self.assertEqual("src/old.py", committed["payload"]["entries"][0]["path"])

        (self.root / "src" / "old.py").write_text("dirty baseline\n", encoding="utf-8")
        dirty_baseline = change_drift.baseline("AR-0001", 4, "now", self.root, ["src"])
        (self.root / "src" / "old.py").write_text("dirty delta\n", encoding="utf-8")
        dirty_delta = change_drift.delta("AR-0001", 5, "now", self.root, dirty_baseline, ["src"])
        self.assertEqual("src/old.py", dirty_delta["payload"]["entries"][0]["path"])
        self.assertNotEqual(
            dirty_baseline["payload"]["entries"][0]["content_digest"],
            dirty_delta["payload"]["entries"][0]["content_digest"],
        )

    def test_committed_rename_preserves_its_pair(self) -> None:
        baseline = change_drift.baseline("AR-0001", 2, "now", self.root, ["src"])
        git(self.root, "mv", "src/old.py", "src/new.py")
        git(self.root, "commit", "-m", "rename")
        record = change_drift.delta("AR-0001", 3, "now", self.root, baseline, ["src"])
        entry = record["payload"]["entries"][0]
        self.assertEqual("src/new.py", entry["path"])
        self.assertEqual("src/old.py", entry["previous_path"])

    def test_hostile_paths_invalid_records_and_stale_explanations_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsafe"):
            change_drift._status_entries(b"?? ../escape\0")
        with self.assertRaisesRegex(ValueError, "safe paths"):
            change_drift.normalize_scope(["../escape"])
        baseline = change_drift.baseline("AR-0001", 2, "now", self.root, ["src"])
        (self.root / "other.txt").write_text("x", encoding="utf-8")
        current = change_drift.delta("AR-0001", 3, "now", self.root, baseline, ["src"])
        stale = change_drift.explain(
            "AR-0001", 4, "now", "sha256:" + "0" * 64, ["other.txt"], "old", False
        )
        with self.assertRaisesRegex(ValueError, "not linked"):
            change_drift.records({"id": "AR-0001", "change_drift": [baseline, current, stale]})
        broken = dict(baseline)
        broken["digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "identity"):
            change_drift.validate(broken)

    def test_rescope_is_bounded_and_latest_scope_wins(self) -> None:
        baseline = change_drift.baseline("AR-0001", 2, "now", self.root, ["src"])
        widened = change_drift.rescope("AR-0001", 3, "now", baseline["digest"], ["src", "docs"])
        self.assertEqual(["docs", "src"], change_drift.active_scope([baseline, widened], baseline))
        with self.assertRaisesRegex(ValueError, "duplicates"):
            change_drift.rescope("AR-0001", 3, "now", baseline["digest"], ["src", "src"])

    def test_history_is_linked_completion_state_machine(self) -> None:
        baseline = change_drift.baseline("AR-0001", 2, "now", self.root, ["src"])
        (self.root / "src" / "old.py").write_text("changed\n", encoding="utf-8")
        delta = change_drift.delta("AR-0001", 3, "now", self.root, baseline, ["src"])
        bypass = change_drift.baseline("AR-0001", 4, "now", self.root, ["src"], delta["digest"])
        with self.assertRaisesRegex(ValueError, "bypasses unresolved"):
            change_drift.records({"id": "AR-0001", "change_drift": [baseline, delta, bypass]})

        unrelated = change_drift.explain(
            "AR-0001", 4, "now", delta["digest"], ["src/missing.py"], "hostile", False
        )
        with self.assertRaisesRegex(ValueError, "unrelated"):
            change_drift.records({"id": "AR-0001", "change_drift": [baseline, delta, unrelated]})

        explanation = change_drift.explain(
            "AR-0001", 4, "now", delta["digest"], ["src/old.py"], "reviewed", False
        )
        history = [baseline, delta, explanation]
        change_drift.records({"id": "AR-0001", "change_drift": history})
        self.assertIsNone(change_drift.completion_error(history))

    def test_next_record_preserves_lifecycle_action_order(self) -> None:
        baseline, history = change_drift.next_record(
            "AR-0001", 2, "now", self.root, [], "baseline", scope_paths=["src"]
        )
        self.assertEqual([], history)
        (self.root / "src" / "old.py").write_text("changed\n", encoding="utf-8")
        delta, history = change_drift.next_record(
            "AR-0001", 3, "now", self.root, [baseline], "delta"
        )
        explanation, history = change_drift.next_record(
            "AR-0001",
            4,
            "now",
            self.root,
            [*history, delta],
            "explain",
            paths=["src/old.py"],
            reason="reviewed",
        )
        self.assertEqual([], change_drift.unresolved([baseline, delta, explanation]))
        with self.assertRaisesRegex(ValueError, "unknown"):
            change_drift.next_record("AR-0001", 5, "now", self.root, [], "unknown")

    def test_status_and_committed_parser_reject_hostile_or_truncated_git_output(self) -> None:
        with self.assertRaisesRegex(ValueError, "malformed porcelain"):
            change_drift._status_entries(b"M bad\0")
        with self.assertRaisesRegex(ValueError, "truncated rename"):
            change_drift._status_entries(b"R  src/new.py\0")

        completed = subprocess.CompletedProcess(
            ["git"], 0, stdout=b"R100\0src/old.py\0src/new.py\0"
        )
        with patch("tools.change_drift.subprocess.run", return_value=completed):
            entries = change_drift._head_entries(self.root, "one", "two")
        self.assertEqual(
            [("src/new.py", "src/old.py", "R ")],
            [(entry["path"], entry["previous_path"], entry["status"]) for entry in entries],
        )

        malformed = subprocess.CompletedProcess(["git"], 0, stdout=b"M\0")
        with (
            patch("tools.change_drift.subprocess.run", return_value=malformed),
            self.assertRaisesRegex(ValueError, "malformed committed"),
        ):
            change_drift._head_entries(self.root, "one", "two")
        failed = subprocess.CompletedProcess(["git"], 1, stdout=b"")
        with (
            patch("tools.change_drift.subprocess.run", return_value=failed),
            self.assertRaisesRegex(RuntimeError, "could not compare"),
        ):
            change_drift._head_entries(self.root, "one", "two")

    def test_lifecycle_rejects_reopen_order_and_unfinished_completion(self) -> None:
        baseline = change_drift.baseline("AR-0001", 2, "now", self.root, ["src"])
        with self.assertRaisesRegex(RuntimeError, "requires a baseline"):
            change_drift.next_record("AR-0001", 3, "now", self.root, [], "delta")
        with self.assertRaisesRegex(RuntimeError, "requires a delta"):
            change_drift.next_record("AR-0001", 3, "now", self.root, [baseline], "explain")
        with self.assertRaisesRegex(RuntimeError, "requires a baseline"):
            change_drift.next_record("AR-0001", 3, "now", self.root, [], "rescope")

        self.assertEqual(
            "CHANGE_DRIFT_DELTA_REQUIRED: active baseline requires a post-work delta",
            change_drift.completion_error([baseline]),
        )
        with self.assertRaisesRegex(RuntimeError, "requires completion"):
            change_drift.next_record(
                "AR-0001", 3, "now", self.root, [baseline], "baseline", scope_paths=["src"]
            )

        (self.root / "src" / "old.py").write_text("changed\n", encoding="utf-8")
        delta = change_drift.delta("AR-0001", 3, "now", self.root, baseline, ["src"])
        self.assertEqual(
            "CHANGE_DRIFT_UNEXPLAINED", change_drift.completion_error([baseline, delta])
        )
        explanation = change_drift.explain(
            "AR-0001", 3, "now", delta["digest"], ["src/old.py"], "reviewed", False, delta["digest"]
        )
        with self.assertRaisesRegex(ValueError, "revisions are not increasing"):
            change_drift.records({"id": "AR-0001", "change_drift": [baseline, delta, explanation]})

    def test_record_validation_rejects_tampered_payloads_and_bad_explanations(self) -> None:
        baseline = change_drift.baseline("AR-0001", 2, "now", self.root, ["src"])
        malformed_scope = dict(baseline)
        malformed_scope["payload"] = {**baseline["payload"], "scope": ["src", "src"]}
        malformed_scope["digest"] = change_drift.digest(
            {
                key: malformed_scope[key]
                for key in (
                    "schema_version",
                    "kind",
                    "task",
                    "task_revision",
                    "payload",
                    "previous_digest",
                )
            }
        )
        with self.assertRaisesRegex(ValueError, "baseline payload"):
            change_drift.validate(malformed_scope)
        with self.assertRaisesRegex(ValueError, "explanation paths"):
            change_drift.explain("AR-0001", 3, "now", baseline["digest"], [], "why", False)
        with self.assertRaisesRegex(ValueError, "reason"):
            change_drift.explain(
                "AR-0001", 3, "now", baseline["digest"], ["src/old.py"], "two\nlines", False
            )
        with self.assertRaisesRegex(ValueError, "baseline digest"):
            change_drift.rescope("AR-0001", 3, "now", "not-a-digest", ["src"])

    def test_history_rejects_cross_task_unlinked_rescope_and_duplicate_delta(self) -> None:
        baseline = change_drift.baseline("AR-0001", 2, "now", self.root, ["src"])
        wrong_task = change_drift.baseline(
            "AR-0002", 3, "now", self.root, ["src"], baseline["digest"]
        )
        with self.assertRaisesRegex(ValueError, "differs from task"):
            change_drift.records({"id": "AR-0001", "change_drift": [baseline, wrong_task]})

        bad_rescope = change_drift.rescope(
            "AR-0001", 3, "now", "sha256:" + "1" * 64, ["src"], baseline["digest"]
        )
        with self.assertRaisesRegex(ValueError, "not linked to the active baseline"):
            change_drift.records({"id": "AR-0001", "change_drift": [baseline, bad_rescope]})

        (self.root / "src" / "old.py").write_text("changed\n", encoding="utf-8")
        first = change_drift.delta("AR-0001", 3, "now", self.root, baseline, ["src"])
        second = change_drift.delta(
            "AR-0001", 4, "now", self.root, baseline, ["src"], first["digest"]
        )
        with self.assertRaisesRegex(ValueError, "delta is not linked"):
            change_drift.records({"id": "AR-0001", "change_drift": [baseline, first, second]})

    def test_command_boundary_applies_fenced_delta_and_rejects_wrong_owner(self) -> None:
        baseline = change_drift.baseline("AR-0001", 2, "now", self.root, ["src"])
        meta: change_drift.Meta = {
            "id": "AR-0001",
            "owner": "worker",
            "status": "in_progress",
            "task_revision": 2,
            "worktree_key": self.root.name,
            "change_drift": [baseline],
        }
        (self.root / "src" / "old.py").write_text("changed\n", encoding="utf-8")
        args = argparse.Namespace(
            task="AR-0001",
            owner="worker",
            expected_revision=2,
            change_drift_action="delta",
            scope_path=None,
            path=None,
            reason=None,
            generated=False,
        )
        result = change_drift.apply(args, meta, self.root.parent, "later")
        self.assertIn("Recorded change-drift delta", result)
        self.assertEqual(2, len(meta["change_drift"]))
        args.owner = "other"
        with self.assertRaisesRegex(RuntimeError, "active task owner"):
            change_drift.apply(args, meta, self.root.parent, "later")

    def test_command_parser_requires_complete_action_arguments(self) -> None:
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        change_drift.add_command_parser(commands)
        values = parser.parse_args(
            [
                "change-drift",
                "explain",
                "AR-0001",
                "--owner",
                "worker",
                "--expected-revision",
                "2",
                "--path",
                "src/old.py",
                "--reason",
                "reviewed",
            ]
        )
        self.assertEqual("change-drift", values.command)
        self.assertEqual("explain", values.change_drift_action)
        with self.assertRaises(SystemExit):
            parser.parse_args(["change-drift", "baseline", "AR-0001"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
