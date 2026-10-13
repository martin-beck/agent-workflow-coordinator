# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Contract coverage for opt-in autonomy budgets and local dispositions."""

from __future__ import annotations

import argparse
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from tools import autonomy_limits

NOW = "2026-10-13T12:00:00+00:00"
LATER = "2026-10-13T12:00:05+00:00"


def git(*arguments: str) -> None:
    subprocess.run(["/usr/bin/git", *arguments], check=True)  # noqa: S603


def args(**values: object) -> argparse.Namespace:
    base = {
        "task": "AR-0001",
        "owner": "solo",
        "expected_revision": 2,
        "autonomy_diff_bytes": 0,
        "autonomy_returncode": 0,
        "cost_units": 0,
        "external_side_effect": False,
        "command_class": None,
    }
    base.update(values)
    return argparse.Namespace(**base)


class AutonomyLimitsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.meta: autonomy_limits.Meta = {
            "id": "AR-0001",
            "owner": "solo",
            "status": "in_progress",
            "task_revision": 2,
        }

    def test_command_overrun_holds_and_solo_disposition_is_revision_fenced(self) -> None:
        selected = {"schema_version": 1, "max_commands": 1, "digest": "sha256:" + "a" * 64}
        self.assertIn("admitted", autonomy_limits.apply_preflight(args(), self.meta, selected, NOW))
        self.meta["task_revision"] = 3
        self.assertIn("hold", autonomy_limits.apply_record(args(), self.meta, selected, LATER))
        self.meta["task_revision"] = 4
        self.assertEqual(4, self.meta["autonomy_limits"]["hold"]["task_revision"])
        with self.assertRaisesRegex(RuntimeError, "stale revision"):
            autonomy_limits.apply_dispose(
                args(expected_revision=3, reason="scope changed"), self.meta, LATER
            )
        self.assertIn(
            "disposition",
            autonomy_limits.apply_dispose(
                args(expected_revision=4, reason="scope changed"), self.meta, LATER
            ),
        )
        self.assertIsNone(self.meta["autonomy_limits"]["hold"])

    def test_elapsed_retry_cost_diff_and_external_triggers_are_diagnostic(self) -> None:
        cases = (
            ({"max_elapsed_seconds": 1}, args(), LATER, "elapsed-time"),
            ({"max_retries": 1}, args(autonomy_returncode=1), LATER, "retry"),
            ({"max_cost_units": 2}, args(cost_units=2), LATER, "cost"),
            ({"max_diff_bytes": 3}, args(autonomy_diff_bytes=2), LATER, "diff-size"),
            (
                {"max_external_side_effects": 1},
                args(external_side_effect=True),
                LATER,
                "external-side-effect",
            ),
        )
        for index, (limits, command, at, trigger) in enumerate(cases):
            with self.subTest(trigger=trigger):
                meta = {
                    "id": "AR-0001",
                    "owner": "solo",
                    "status": "in_progress",
                    "task_revision": 2,
                }
                selected = {"schema_version": 1, **limits, "digest": "sha256:" + f"{index:x}" * 64}
                autonomy_limits.apply_preflight(command, meta, selected, NOW)
                meta["task_revision"] = 3
                completed = args(**vars(command))
                if trigger == "diff-size":
                    completed.autonomy_diff_bytes = 3
                autonomy_limits.apply_record(completed, meta, selected, at)
                state = cast(autonomy_limits.Meta, meta["autonomy_limits"])
                hold = cast(autonomy_limits.Meta, state["hold"])
                self.assertEqual(trigger, hold["trigger"])

    def test_preflight_breach_returns_a_persistable_hold(self) -> None:
        selected = {"schema_version": 1, "max_elapsed_seconds": 1, "digest": "sha256:" + "c" * 64}
        command = args()
        note = autonomy_limits.apply_preflight(command, self.meta, selected, NOW)
        self.assertIn("admitted", note)
        self.meta["task_revision"] = 3
        held = autonomy_limits.apply_preflight(command, self.meta, selected, LATER)
        self.assertIn("hold recorded", held)
        self.assertTrue(command.autonomy_limit_held)

    def test_tampering_and_bypass_attempts_fail_closed(self) -> None:
        selected = {"schema_version": 1, "max_commands": 2, "digest": "sha256:" + "a" * 64}
        autonomy_limits.apply_preflight(args(), self.meta, selected, NOW)
        self.meta["autonomy_limits"]["hold"] = {
            "trigger": "command",
            "observed": 2,
            "limit": 2,
            "held_at": NOW,
            "task_revision": 99,
        }
        self.assertIn("invalid", autonomy_limits.metadata_error(self.meta) or "")
        self.meta["autonomy_limits"]["hold"] = None
        self.meta["autonomy_limits"]["policy_digest"] = "sha256:" + "b" * 64
        with self.assertRaisesRegex(RuntimeError, "POLICY_MISMATCH"):
            autonomy_limits.apply_record(args(), self.meta, selected, LATER)
        with self.assertRaisesRegex(RuntimeError, "cost units"):
            autonomy_limits.apply_record(args(cost_units=-1), {"id": "AR-0001"}, selected, LATER)

    def test_policy_requires_clean_tracked_regular_file_and_detects_change(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            git("init", "-q", str(root))
            git("-C", str(root), "config", "user.email", "test@example.invalid")
            git("-C", str(root), "config", "user.name", "Test")
            path = root / autonomy_limits.POLICY_NAME
            path.write_text(json.dumps({"schema_version": 1, "max_commands": 2}) + "\n")
            git("-C", str(root), "add", path.name)
            git("-C", str(root), "commit", "-qm", "policy")
            selected = autonomy_limits.policy(root)
            if selected is None:
                self.fail("tracked autonomy limits policy was not loaded")
            self.assertEqual(2, selected["max_commands"])
            path.write_text(path.read_text() + " ")
            with self.assertRaisesRegex(autonomy_limits.PolicyError, "tracked and unchanged"):
                autonomy_limits.policy(root)

    def test_missing_policy_is_opt_in_absent_but_tracked_deletion_fails_closed(self) -> None:
        """An absent opt-in policy differs from deleting a tracked policy."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            git("init", "-q", str(root))
            git("-C", str(root), "config", "user.email", "test@example.invalid")
            git("-C", str(root), "config", "user.name", "Test")
            git("-C", str(root), "commit", "--allow-empty", "-qm", "empty")
            self.assertIsNone(autonomy_limits.policy(root))

            path = root / autonomy_limits.POLICY_NAME
            path.write_text(json.dumps({"schema_version": 1, "max_commands": 2}) + "\n")
            git("-C", str(root), "add", path.name)
            git("-C", str(root), "commit", "-qm", "policy")
            path.unlink()
            with self.assertRaisesRegex(autonomy_limits.PolicyError, "tracked and unchanged"):
                autonomy_limits.policy(root)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
