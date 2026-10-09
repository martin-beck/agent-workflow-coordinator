# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Safety checks for 16-lane Git-backed mixed-route classification."""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from git_mixed_command_probe import (
    adversarial_commands,
    checked_batch,
    error_class,
    require_unchanged_sources,
    route_name,
    stabilize_disposable_claims,
)


class GitMixedCommandProbeTests(unittest.TestCase):
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
