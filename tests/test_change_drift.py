# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Contract coverage for bounded, revision-fenced change-drift records."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
