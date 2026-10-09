# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Safety checks for 16-lane Git-backed mixed-route classification."""

import datetime as dt
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from git_mixed_command_probe import (
    TASK_IDS,
    acceptance_errors,
    acceptances,
    adversarial_commands,
    checked_batch,
    error_class,
    require_unchanged_sources,
    route_name,
    stabilize_disposable_claims,
    strict_acceptance_commit_errors,
)


class GitMixedCommandProbeTests(unittest.TestCase):
    def test_acceptance_commit_checker_rejects_unsigned_or_missing_dco(self) -> None:
        def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
            if argv[1] == "rev-list":
                return subprocess.CompletedProcess(argv, 0, "abc123\n")
            if argv[1] == "diff":
                return subprocess.CompletedProcess(argv, 0, b"tasks/AR-9000.md\0")
            if argv[1] == "verify-commit":
                return subprocess.CompletedProcess(argv, 1, b"", b"invalid signature")
            if argv[1] == "show":
                return subprocess.CompletedProcess(argv, 0, "missing trailer")
            raise AssertionError(argv)

        with (
            mock.patch("git_mixed_command_probe.subprocess.run", side_effect=fake_run),
            mock.patch("git_mixed_command_probe.history_extends", return_value=True),
        ):
            count, errors = strict_acceptance_commit_errors(Path("disposable"), "starting")
        self.assertEqual(1, count)
        self.assertTrue(any("signature did not verify" in error for error in errors))
        self.assertTrue(any("DCO trailer missing" in error for error in errors))

    def test_acceptance_errors_reject_missing_or_wrong_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tasks = Path(directory) / "tasks"
            tasks.mkdir()
            before: dict[str, dict[str, object]] = {}
            for index, task_id in enumerate(TASK_IDS):
                meta = {
                    "owner": f"bench-{index}",
                    "task_revision": 3,
                    "spec_ref": f"specs/{task_id}.json",
                    "spec_revision": 1,
                    "spec_acceptance": {
                        "spec_ref": f"specs/{task_id}.json",
                        "spec_revision": 1,
                        "status": "pass",
                        "evidence_class": "mechanical",
                        "evidence_ref": f"quality/{task_id}",
                        "evidence_digest": "sha256:" + "a" * 64,
                    },
                }
                before[task_id] = {
                    key: value for key, value in meta.items() if key != "spec_acceptance"
                }
                before[task_id]["task_revision"] = 2
                (tasks / f"{task_id}.md").write_text("---\n" + json.dumps(meta) + "\n---\n")
            self.assertEqual([], acceptance_errors(Path(directory), before))
            bad = tasks / f"{TASK_IDS[0]}.md"
            meta = json.loads(bad.read_text().split("---", 2)[1])
            del meta["spec_acceptance"]
            bad.write_text("---\n" + json.dumps(meta) + "\n---\n")
            self.assertIn(
                "acceptance does not match request", acceptance_errors(Path(directory), before)[0]
            )
            meta["unrelated"] = "changed"
            bad.write_text("---\n" + json.dumps(meta) + "\n---\n")
            self.assertTrue(
                any(
                    "unrelated task fields changed" in error
                    for error in acceptance_errors(Path(directory), before)
                )
            )

    def test_acceptances_bind_each_owner_revision_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tasks = Path(directory) / "tasks"
            tasks.mkdir()
            for index, task_id in enumerate(TASK_IDS):
                (tasks / f"{task_id}.md").write_text(
                    "---\n" + json.dumps({"task_revision": index + 2}) + "\n---\n"
                )
            commands = acceptances(Path(directory))
            self.assertEqual(16, len(commands))
            self.assertEqual(16, len({tuple(command) for command in commands}))
            for index, command in enumerate(commands):
                self.assertEqual(["accept", TASK_IDS[index]], command[:2])
                self.assertEqual(f"bench-{index}", command[3])
                self.assertEqual(str(index + 2), command[5])
                self.assertEqual("mechanical", command[7])
                self.assertEqual(f"quality/{TASK_IDS[index]}", command[9])

    def test_stabilize_claims_only_changes_disposable_active_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tasks = Path(directory) / "tasks"
            tasks.mkdir()
            claimed = tasks / "AR-9000.md"
            open_task = tasks / "AR-9001.md"
            future_task = tasks / "AR-9002.md"
            expired = "2020-01-01T00:00:00+00:00"
            claimed.write_text(
                "---\n"
                + json.dumps({"status": "in_progress", "claim_expires": expired}, sort_keys=True)
                + "\n---\n\nKeep this body.\n"
            )
            open_task.write_text(
                "---\n"
                + json.dumps({"status": "open", "claim_expires": ""}, sort_keys=True)
                + "\n---\n\nKeep this open task.\n"
            )
            future = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=2)).isoformat()
            future_task.write_text(
                "---\n"
                + json.dumps({"status": "in_progress", "claim_expires": future}, sort_keys=True)
                + "\n---\n\nKeep this future task.\n"
            )
            before_open = open_task.read_bytes()
            before_future = future_task.read_bytes()
            self.assertEqual(1, stabilize_disposable_claims(Path(directory)))
            self.assertEqual(before_open, open_task.read_bytes())
            self.assertEqual(before_future, future_task.read_bytes())
            self.assertTrue(claimed.read_text().endswith("\n\nKeep this body.\n"))
            metadata = json.loads(claimed.read_text().split("---", 2)[1])
            self.assertGreater(
                dt.datetime.fromisoformat(metadata["claim_expires"]), dt.datetime.now(dt.UTC)
            )

    def test_stabilize_claims_rejects_malformed_source_without_rewriting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tasks = Path(directory) / "tasks"
            tasks.mkdir()
            expired = tasks / "AR-9000.md"
            malformed = tasks / "AR-9001.md"
            expired.write_text(
                "---\n"
                + json.dumps(
                    {"status": "in_progress", "claim_expires": "2020-01-01T00:00:00+00:00"}
                )
                + "\n---\n"
            )
            malformed.write_text(
                "---\n"
                + json.dumps({"status": "in_progress", "claim_expires": "invalid"})
                + "\n---\n"
            )
            before = expired.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "invalid source claim expiry"):
                stabilize_disposable_claims(Path(directory))
            self.assertEqual(before, expired.read_bytes())

    def test_route_names_keep_semantically_distinct_variants(self) -> None:
        self.assertEqual("doctor-live", route_name(["doctor", "--live"]))
        self.assertEqual("doctor", route_name(["doctor"]))
        self.assertEqual("render-check", route_name(["render-status", "--check"]))
        self.assertEqual("roles-assign", route_name(["roles", "assign"]))

    def test_expected_rejections_are_not_success(self) -> None:
        self.assertEqual("lock_timeout", error_class(b"LOCK_TIMEOUT"))
        self.assertEqual("wrong_owner", error_class(b"AR-9000 is owned by bench-0"))
        self.assertEqual("wrong_owner", error_class(b"task claim does not match owner"))
        self.assertEqual("stale_task_revision", error_class(b"stale revision: expected 2"))
        self.assertEqual("malformed_gate_artifact", error_class(b"--before must use REF=DIGEST"))
        self.assertEqual("stale_role_revision", error_class(b"stale role revision"))
        self.assertEqual(
            "unsupported_git_route", error_class(b"board requires the SQLite authority")
        )
        self.assertEqual("usage_error", error_class(b"usage: handoffctl"))

    def test_adversarial_batch_has_one_legitimate_worker(self) -> None:
        commands = adversarial_commands(4, Path("disposable-unauthorized-marker"))
        self.assertEqual(16, len(commands))
        self.assertEqual(
            ["update", "AR-9000", "--owner", "bench-0", "--expected-revision", "4"],
            commands[-1][:6],
        )
        self.assertEqual(15, sum(command != commands[-1] for command in commands))
        self.assertEqual(2, sum("write_text" in " ".join(command) for command in commands))

    def test_source_drift_fails_probe(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "source inputs changed"):
            require_unchanged_sources(
                {"source_product_inputs_changed": True, "source_state_head_changed": False}
            )

    def test_mixed_batch_fails_on_lost_worker_or_bad_doctor(self) -> None:
        with (
            mock.patch(
                "git_mixed_command_probe.batch", return_value={"routes": {"claim": {"ok": 15}}}
            ),
            mock.patch(
                "git_mixed_command_probe.doctor", return_value={"doctor": 0, "doctor_live": 0}
            ),
            self.assertRaisesRegex(RuntimeError, "unexpected route outcome"),
        ):
            checked_batch(Path("disposable-state"), {}, "claims", [], 30, 16)
        with (
            mock.patch(
                "git_mixed_command_probe.batch", return_value={"routes": {"claim": {"ok": 16}}}
            ),
            mock.patch(
                "git_mixed_command_probe.doctor", return_value={"doctor": 0, "doctor_live": 1}
            ),
            self.assertRaisesRegex(RuntimeError, "failed integrity check"),
        ):
            checked_batch(Path("disposable-state"), {}, "claims", [], 30, 16)


if __name__ == "__main__":
    unittest.main()
