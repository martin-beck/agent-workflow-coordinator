# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Contract coverage for bounded, revision-fenced change-drift records."""

from __future__ import annotations

import argparse
import contextlib
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools import change_drift, handoffctl


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

    def test_top_level_cli_routes_each_fenced_change_drift_action(self) -> None:
        cases: tuple[tuple[list[str], dict[str, object]], ...] = (
            (["baseline", "AR-0001", "--scope-path", "src"], {"scope_path": ["src"]}),
            (["delta", "AR-0001"], {}),
            (
                ["rescope", "AR-0001", "--scope-path", "src", "--scope-path", "docs"],
                {"scope_path": ["src", "docs"]},
            ),
            (
                [
                    "explain",
                    "AR-0001",
                    "--path",
                    "src/old.py",
                    "--reason",
                    "intentional update",
                    "--generated",
                ],
                {"path": ["src/old.py"], "generated": True},
            ),
        )
        for action, expected in cases:
            with self.subTest(action=action[0]):
                with (
                    patch.object(handoffctl, "assert_project_binding"),
                    patch.object(handoffctl, "dispatch_bound_command", return_value=0) as dispatch,
                    patch(
                        "sys.argv",
                        [
                            "handoffctl",
                            "change-drift",
                            *action,
                            "--owner",
                            "worker",
                            "--expected-revision",
                            "2",
                        ],
                    ),
                ):
                    self.assertEqual(0, handoffctl.main())
                parsed = dispatch.call_args.args[0]
                self.assertEqual("change-drift", parsed.cmd)
                self.assertEqual(action[0], parsed.change_drift_action)
                self.assertEqual("worker", parsed.owner)
                self.assertEqual(2, parsed.expected_revision)
                for name, value in expected.items():
                    self.assertEqual(value, getattr(parsed, name))

    def test_observation_and_fingerprinting_fail_closed_on_unusable_git_results(self) -> None:
        missing = change_drift._content_digest(self.root, "src/missing.py")
        self.assertEqual("", missing)
        bad_fingerprint = subprocess.CompletedProcess(["git"], 0, stdout="not-a-hash\n")
        with (
            patch("tools.change_drift.subprocess.run", return_value=bad_fingerprint),
            self.assertRaisesRegex(RuntimeError, "could not fingerprint"),
        ):
            change_drift._content_digest(self.root, "src/old.py")
        failed_status = subprocess.CompletedProcess(["git"], 1, stdout=b"")
        head = subprocess.CompletedProcess(["git"], 1, stdout="")
        with (
            patch("tools.change_drift.subprocess.run", side_effect=[head, failed_status]),
            self.assertRaisesRegex(RuntimeError, "requires a Git worktree"),
        ):
            change_drift.observe(self.root)
        renamed = change_drift._status_entries(b"R  src/new.py\0src/old.py\0")
        self.assertEqual("src/old.py", renamed[0]["previous_path"])

    def test_validation_rejects_hostile_observations_and_payload_kinds(self) -> None:
        baseline = change_drift.baseline("AR-0001", 2, "now", self.root, ["src"])
        invalid_entry = dict(baseline)
        invalid_entry["payload"] = {
            **baseline["payload"],
            "entries": [
                {
                    "path": "../escape",
                    "previous_path": "",
                    "status": "??",
                    "generated": False,
                    "content_digest": "",
                }
            ],
        }
        self._rehash(invalid_entry)
        with self.assertRaisesRegex(ValueError, "observation entry"):
            change_drift.validate(invalid_entry)

        malformed_delta = change_drift._record(
            "delta",
            "AR-0001",
            3,
            "now",
            {"baseline_digest": baseline["digest"], "head": 7, "entries": []},
        )
        with self.assertRaisesRegex(ValueError, "observation payload"):
            change_drift.validate(malformed_delta)

        malformed_explain = change_drift._record(
            "explain",
            "AR-0001",
            3,
            "now",
            {
                "delta_digest": baseline["digest"],
                "paths": ["src/old.py", "src/old.py"],
                "reason": "ok",
                "generated": False,
            },
        )
        with self.assertRaisesRegex(ValueError, "explanation payload"):
            change_drift.validate(malformed_explain)

    def test_history_command_boundary_and_capacity_reject_stale_or_invalid_mutations(self) -> None:
        baseline = change_drift.baseline("AR-0001", 2, "now", self.root, ["src"])
        with self.assertRaisesRegex(ValueError, "invalid change-drift history"):
            change_drift.records({"id": "AR-0001", "change_drift": "not-a-list"})
        unlinked = dict(baseline)
        unlinked["previous_digest"] = "sha256:" + "f" * 64
        self._rehash(unlinked)
        with self.assertRaisesRegex(ValueError, "history is not linked"):
            change_drift.records({"id": "AR-0001", "change_drift": [unlinked]})

        meta: change_drift.Meta = {
            "id": "AR-0001",
            "owner": "worker",
            "status": "in_progress",
            "task_revision": 2,
            "change_drift": [baseline],
        }
        stale = argparse.Namespace(
            task="AR-0001",
            owner="worker",
            expected_revision=1,
            change_drift_action="delta",
            scope_path=None,
            path=None,
            reason=None,
            generated=False,
        )
        with self.assertRaisesRegex(RuntimeError, "stale revision"):
            change_drift.apply(stale, meta, self.root.parent, "later")
        capped = [baseline] * (change_drift.MAX_RECORDS + 1)
        with self.assertRaisesRegex(ValueError, "invalid change-drift history"):
            change_drift.records({"id": "AR-0001", "change_drift": capped})

    def test_record_parsers_and_canonicality_reject_malformed_boundary_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "record identity"):
            change_drift._record("unknown", "AR-0001", 1, "now", {})
        with self.assertRaisesRegex(ValueError, "predecessor"):
            change_drift._record("baseline", "AR-0001", 1, "now", {}, "not-a-digest")

        empty = subprocess.CompletedProcess(["git"], 0, stdout=b"\0")
        with patch("tools.change_drift.subprocess.run", return_value=empty):
            self.assertEqual([], change_drift._head_entries(self.root, "one", "two"))
        unsafe = subprocess.CompletedProcess(["git"], 0, stdout=b"M\0../escape\0")
        with (
            patch("tools.change_drift.subprocess.run", return_value=unsafe),
            self.assertRaisesRegex(ValueError, "unsafe committed"),
        ):
            change_drift._head_entries(self.root, "one", "two")

        baseline = change_drift.baseline("AR-0001", 2, "now", self.root, ["src"])
        entry = {
            "path": "src/a.py",
            "previous_path": "",
            "status": "??",
            "generated": False,
            "content_digest": "",
        }
        extra = {**entry, "path": "src/z.py"}
        malformed = dict(baseline)
        malformed["payload"] = {**baseline["payload"], "entries": [extra, entry]}
        self._rehash(malformed)
        with self.assertRaisesRegex(ValueError, "not canonical"):
            change_drift.validate(malformed)

    def test_lifecycle_delta_and_rescope_edge_cases_remain_bounded(self) -> None:
        baseline = change_drift.baseline("AR-0001", 2, "now", self.root, ["src"])
        with self.assertRaisesRegex(RuntimeError, "requires completion"):
            change_drift.next_record(
                "AR-0001", 3, "now", self.root, [baseline], "baseline", scope_paths=["src"]
            )
        with self.assertRaisesRegex(RuntimeError, "requires a baseline"):
            change_drift.next_record("AR-0001", 3, "now", self.root, [], "delta", scope_paths=[])
        rescope = change_drift.rescope(
            "AR-0001", 3, "now", baseline["digest"], ["docs", "src"], baseline["digest"]
        )
        self.assertEqual(["docs", "src"], rescope["payload"]["scope"])
        malformed = dict(rescope)
        malformed["payload"] = {**rescope["payload"], "scope": ["src", "docs"]}
        self._rehash(malformed)
        with self.assertRaisesRegex(ValueError, "re-scope payload"):
            change_drift.validate(malformed)

    def test_done_admission_and_transition_route_change_drift_only(self) -> None:
        invalid = {"id": "AR-0001", "status": "in_progress", "change_drift": "hostile"}
        with (
            patch.object(handoffctl, "done_admission_error", return_value=None),
            self.assertRaisesRegex(RuntimeError, "change-drift record is invalid"),
        ):
            handoffctl.require_done_admission(invalid)

        baseline = change_drift.baseline("AR-0001", 2, "now", self.root, ["src"])
        (self.root / "src" / "old.py").write_text("changed\n", encoding="utf-8")
        delta = change_drift.delta("AR-0001", 3, "now", self.root, baseline, ["src"])
        meta = {"id": "AR-0001", "status": "in_progress", "change_drift": [baseline, delta]}
        with (
            patch.object(handoffctl, "done_admission_error", return_value=None),
            self.assertRaisesRegex(RuntimeError, "CHANGE_DRIFT_UNEXPLAINED"),
        ):
            handoffctl.require_done_admission(meta)

        args = argparse.Namespace(task="AR-0001", change_drift_action="baseline")
        meta_for_transition: dict[str, object] = {"id": "AR-0001"}
        with (
            patch.object(
                handoffctl, "config", return_value={"projects_root": str(self.root.parent)}
            ),
            patch.object(handoffctl, "now", return_value="now"),
            patch.object(change_drift, "apply", return_value="recorded") as apply,
        ):
            self.assertEqual(
                "recorded",
                handoffctl.apply_transition(args, "change-drift", meta_for_transition, []),
            )
        self.assertEqual(args, apply.call_args.args[0])
        self.assertEqual(meta_for_transition, apply.call_args.args[1])
        self.assertEqual(self.root.parent, apply.call_args.args[2])
        self.assertEqual("now", apply.call_args.args[3])

    def test_validator_rejects_exact_shape_baseline_and_delta_contract_violations(self) -> None:
        baseline = change_drift.baseline("AR-0001", 2, "now", self.root, ["src"])
        with self.assertRaisesRegex(ValueError, "fields are not exact"):
            change_drift.validate({})

        malformed_payload = dict(baseline)
        malformed_payload["payload"] = []
        self._rehash(malformed_payload)
        with self.assertRaisesRegex(ValueError, "record payload"):
            change_drift.validate(malformed_payload)

        malformed_delta = change_drift._record(
            "delta", "AR-0001", 3, "now", {"head": "head", "entries": [], "extra": True}
        )
        with self.assertRaisesRegex(ValueError, "delta payload"):
            change_drift.validate(malformed_delta)

        malformed_baseline = dict(baseline)
        malformed_baseline["payload"] = {**baseline["payload"], "scope": ["src", "docs"]}
        self._rehash(malformed_baseline)
        with self.assertRaisesRegex(ValueError, "baseline payload"):
            change_drift.validate(malformed_baseline)

    def test_delta_ignores_unchanged_baseline_and_duplicate_committed_observations(self) -> None:
        baseline = change_drift.baseline("AR-0001", 2, "now", self.root, ["src"])
        self.assertEqual(
            [],
            change_drift.delta("AR-0001", 3, "now", self.root, baseline, ["src"])["payload"][
                "entries"
            ],
        )

        entry = {
            "path": "src/old.py",
            "previous_path": "",
            "status": " M",
            "generated": False,
            "content_digest": "same",
        }
        baseline["payload"] = {**baseline["payload"], "entries": [entry]}
        self._rehash(baseline)
        with (
            patch(
                "tools.change_drift.observe",
                return_value={"head": "next", "entries": [entry]},
            ),
            patch("tools.change_drift._head_entries", return_value=[entry]),
        ):
            result = change_drift.delta("AR-0001", 3, "now", self.root, baseline, ["src"])
        self.assertEqual([], result["payload"]["entries"])

    def test_history_rejects_unfinished_cycles_and_rescope_after_delta(self) -> None:
        baseline = change_drift.baseline("AR-0001", 2, "now", self.root, ["src"])
        delta = change_drift._record(
            "delta",
            "AR-0001",
            3,
            "now",
            {"baseline_digest": baseline["digest"], "head": "head", "entries": []},
            baseline["digest"],
        )
        rescope = change_drift.rescope(
            "AR-0001",
            4,
            "now",
            baseline["digest"],
            ["src"],
            delta["digest"],
        )
        with self.assertRaisesRegex(ValueError, "re-scope is not linked"):
            change_drift.records({"id": "AR-0001", "change_drift": [baseline, delta, rescope]})
        with self.assertRaisesRegex(RuntimeError, "re-scope requires a baseline"):
            change_drift.next_record(
                "AR-0001", 4, "now", self.root, [baseline, delta], "rescope", scope_paths=["src"]
            )

    def test_apply_rejects_missing_worktree_and_cycle_capacity(self) -> None:
        args = argparse.Namespace(
            task="AR-0001",
            owner="worker",
            expected_revision=2,
            change_drift_action="baseline",
            scope_path=["src"],
            path=None,
            reason=None,
            generated=False,
        )
        with self.assertRaisesRegex(RuntimeError, "bound worktree"):
            change_drift.apply(
                args,
                {"id": "AR-0001", "owner": "worker", "status": "in_progress", "task_revision": 2},
                self.root.parent,
                "now",
            )
        history: list[change_drift.Meta] = []
        with (
            patch("tools.change_drift.records", return_value=history),
            patch("tools.change_drift.next_record", return_value=({"kind": "baseline"}, history)),
        ):
            history.extend({} for _ in range(change_drift.MAX_RECORDS))
            with self.assertRaisesRegex(RuntimeError, "bounded record limit"):
                change_drift.apply(
                    args,
                    {
                        "id": "AR-0001",
                        "owner": "worker",
                        "status": "in_progress",
                        "task_revision": 2,
                        "worktree_key": self.root.name,
                    },
                    self.root.parent,
                    "now",
                )

    def test_bound_worktree_rejects_traversal_absolute_and_symlink_escape(self) -> None:
        projects_root = self.root / "projects"
        projects_root.mkdir()
        (projects_root / "safe").mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (projects_root / "link").symlink_to(outside, target_is_directory=True)
        (projects_root / "alias").symlink_to("safe", target_is_directory=True)
        with change_drift.bound_worktree({"worktree_key": "safe"}, projects_root) as worktree:
            self.assertEqual("safe", worktree.resolve().name)
        for key in ("../escape", "/outside/escape", "nested/worktree", "link", "alias", ""):
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(RuntimeError, "bound worktree"),
                change_drift.bound_worktree({"worktree_key": key}, projects_root),
            ):
                pass
        with (
            self.assertRaisesRegex(RuntimeError, "requires a bound worktree"),
            change_drift.bound_worktree(
                {"worktree_key": "safe"}, projects_root, product_worktree="safe"
            ),
        ):
            pass

    def test_bound_worktree_descriptor_survives_path_replacement(self) -> None:
        projects_root = self.root / "projects"
        projects_root.mkdir()
        worktree = projects_root / "safe"
        worktree.mkdir()
        (worktree / "inside").write_text("original", encoding="utf-8")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "inside").write_text("attacker", encoding="utf-8")
        parked = projects_root / "parked"
        with change_drift.bound_worktree({"worktree_key": "safe"}, projects_root) as bound:
            worktree.rename(parked)
            worktree.symlink_to(outside, target_is_directory=True)
            self.assertEqual("original", (bound / "inside").read_text(encoding="utf-8"))

    def test_execution_preflight_rejects_persisted_unsafe_worktree_key(self) -> None:
        args = argparse.Namespace(
            task="AR-0001", owner="worker", command_class="source-write", scope_path=[]
        )
        meta = {"id": "AR-0001", "worktree_key": "../outside"}
        with (
            patch.object(handoffctl, "locked", return_value=contextlib.nullcontext()),
            patch.object(handoffctl, "locate", return_value=(Path("task"), meta, "")),
            patch.object(
                handoffctl,
                "config",
                return_value={"projects_root": str(self.root), "product_worktree": "product"},
            ),
        ):
            allowed, code, detail, timeout = handoffctl.execution_preflight(args)
        self.assertEqual((False, "WORKTREE_BINDING_INVALID", None), (allowed, code, timeout))
        self.assertIn("requires a bound worktree", detail)

    def _rehash(self, record: change_drift.Meta) -> None:
        record["digest"] = change_drift.digest(
            {
                key: record[key]
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
