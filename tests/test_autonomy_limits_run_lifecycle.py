# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""End-to-end autonomy-limit lifecycle coverage over both authorities."""

from __future__ import annotations

import argparse
import contextlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import test_handoffctl
from test_handoffctl import CORE, run_git


class AutonomyLimitsRunLifecycleTests(unittest.TestCase):
    """Exercise actual run routing without mocking policy or mutation paths."""

    def setUp(self) -> None:
        self.fixture = test_handoffctl.HandoffTest(
            "test_active_owner_rejects_an_elapsed_lease_before_execution"
        )
        self.fixture.setUp()
        self.root = self.fixture.root
        CORE.CONTROL_DATABASE = CORE.RUNTIME / "coordinator.control.sqlite3"
        CORE.AUTHORITY_MARKER = CORE.RUNTIME / "coordinator.authority-marker.json"
        CORE.AUTHORITY_LIFECYCLE = CORE.RUNTIME / "coordinator.authority-lifecycle.json"
        CORE.AUTHORITY_LOCK = CORE.RUNTIME / "coordinator.authority.lock"
        CORE.CONTROL_BINDING = CORE.RUNTIME / "coordinator.control-binding.json"
        CORE.CONTROL_LOCK = CORE.RUNTIME / ".coordinator.control.sqlite3.lock"

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def prepare_backend(self, backend: str) -> Path:
        self.assertIn(backend, ("git", "sqlite"))
        self.fixture.make_task(
            status="in_progress",
            owner="worker-a",
            claim_expires="2099-01-01T00:00:00+00:00",
        )
        policy = self.root / "autonomy-limits-policy.json"
        policy.write_text(json.dumps({"schema_version": 1, "max_commands": 1}) + "\n")
        CORE.CONFIG.parent.mkdir(exist_ok=True)
        CORE.CONFIG.write_text("{}")
        run_git(["git", "init", "-q", "-b", "main", str(self.root)])
        for key, value in (("user.name", "Fixture"), ("user.email", "fixture@example.invalid")):
            run_git(["git", "-C", str(self.root), "config", key, value])
        run_git(["git", "-C", str(self.root), "add", "--all"])
        run_git(["git", "-C", str(self.root), "commit", "-qm", "autonomy fixture"])
        if backend == "sqlite":
            CORE.cmd_migrate(argparse.Namespace(to="sqlite"))
        return policy

    def assert_lifecycle(self, backend: str) -> None:
        policy = self.prepare_backend(backend)
        first = argparse.Namespace(
            cmd="run", task="AR-0001", owner="worker-a", command=["/usr/bin/true"]
        )
        # Git reconciliation's hosted observation is advisory.  Keep that one
        # input local without replacing policy, command, or mutation routing.
        scan = patch.object(CORE, "project_scan", return_value=self.fixture.fake_scan())
        context = scan if backend == "git" else contextlib.nullcontext()
        with context:
            self.assertEqual(0, CORE.cmd_run(first))
        _, recorded, _ = CORE.locate("AR-0001")
        state = recorded["autonomy_limits"]
        self.assertEqual(1, state["commands"])
        self.assertIsNotNone(state["hold"])

        stale = argparse.Namespace(
            cmd="autonomy-limits",
            autonomy_limits_action="dispose",
            task="AR-0001",
            owner="worker-a",
            expected_revision=int(recorded["task_revision"]) - 1,
            reason="stale disposition",
        )
        with self.assertRaisesRegex(RuntimeError, "stale revision"):
            CORE.al.dispatch(CORE._RUN_RUNTIME, stale)

        marker = self.root / "effect-marker"
        held = argparse.Namespace(
            cmd="run",
            task="AR-0001",
            owner="worker-a",
            command=["/usr/bin/sh", "-c", f"printf effect > {marker}"],
        )
        with self.assertRaisesRegex(RuntimeError, "AUTONOMY_LIMIT_HOLD"):
            CORE.cmd_run(held)
        self.assertFalse(marker.exists())

        original = policy.read_bytes()
        for changed in (b'{"schema_version":1,"max_commands":2}\n', None):
            with self.subTest(backend=backend, changed=changed is not None):
                if changed is None:
                    policy.unlink()
                else:
                    policy.write_bytes(changed)
                with self.assertRaisesRegex(RuntimeError, "tracked and unchanged"):
                    CORE.cmd_run(held)
                self.assertFalse(marker.exists())
                policy.write_bytes(original)

    def test_lifecycle_uses_real_git_backend(self) -> None:
        self.assert_lifecycle("git")

    def test_lifecycle_uses_real_sqlite_backend(self) -> None:
        self.assert_lifecycle("sqlite")
