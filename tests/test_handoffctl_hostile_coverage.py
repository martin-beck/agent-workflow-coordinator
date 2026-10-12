# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Fail-closed unit coverage for coordinator boundary helpers."""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from tools import handoffctl as core


class HandoffctlHostileCoverageTests(unittest.TestCase):
    """Exercise security and recovery rejection paths without authority mutation."""

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.original = {name: getattr(core, name) for name in ("ROOT", "RUNTIME", "LOCK")}
        core.ROOT = self.root
        core.RUNTIME = self.root / ".runtime"
        core.LOCK = core.RUNTIME / "state.lock"

    def tearDown(self) -> None:
        for name, value in self.original.items():
            setattr(core, name, value)
        self.temporary.cleanup()

    def test_parsers_and_task_metadata_reject_malformed_security_fields(self) -> None:
        self.assertEqual([], core.parse_worktree_listing("not a worktree\n\n"))
        self.assertFalse(core.valid_claim_process({"pid": 0, "start_time": 1}))
        self.assertFalse(core.valid_claim_process({"pid": 1, "start_time": -1}))
        self.assertFalse(
            core.valid_claim_process(
                {
                    "pid": 1,
                    "start_time": 1,
                    "boot_digest": "sha256:bad",
                    "pid_namespace": "invalid",
                }
            )
        )
        self.assertEqual(
            ["AR-0001: privacy-unsafe short_title"],
            core.short_title_errors(Path("task.md"), "AR-0001", "/home/private"),
        )
        with (
            patch.object(core, "valid_execution_reservation", return_value=False),
            patch.object(core, "valid_execution_result", return_value=False),
            patch.object(core, "valid_execution_envelope_binding", return_value=False),
        ):
            errors = core.execution_metadata_errors(
                "AR-0001",
                {
                    "execution_reservation": {"untrusted": True},
                    "execution_result": {"untrusted": True},
                },
            )
        self.assertEqual(3, len(errors))

    def test_claim_identity_and_recovery_probes_fail_closed(self) -> None:
        with (
            patch("tools.handoffctl.Path.read_text", side_effect=OSError("denied")),
            self.assertRaisesRegex(
                RuntimeError, "cannot independently read worker execution scope"
            ),
        ):
            core.worker_execution_scope()
        with (
            patch("tools.handoffctl.Path.read_text", return_value="not-a-uuid"),
            self.assertRaisesRegex(RuntimeError, "invalid boot identity"),
        ):
            core.worker_execution_scope()
        with self.assertRaisesRegex(RuntimeError, "requires positive"):
            core.claimed_process_identity(argparse.Namespace(worker_pid=1, worker_start_time=None))
        with (
            patch.object(core, "process_start_time", return_value=None),
            self.assertRaisesRegex(RuntimeError, "not a live matching process"),
        ):
            core.claimed_process_identity(argparse.Namespace(worker_pid=1, worker_start_time=1))
        with self.assertRaisesRegex(RuntimeError, "scope-bound"):
            core.require_claimed_worker_absent({})
        identity = {
            "pid": 1,
            "start_time": 1,
            "boot_digest": "sha256:" + "a" * 64,
            "pid_namespace": "1:1",
        }
        with (
            patch.object(
                core, "worker_execution_scope", return_value=("sha256:" + "b" * 64, "1:1")
            ),
            self.assertRaisesRegex(RuntimeError, "execution scope"),
        ):
            core.require_claimed_worker_absent({"claim_process": identity})
        self.assertFalse(
            core.legacy_recovery_admitted(
                argparse.Namespace(allow_legacy_without_identity=True),
                {"claim_process": identity, "claim_expires": "2000-01-01T00:00:00+00:00"},
            )
        )

    def test_validation_journals_and_replication_reject_unsafe_inputs(self) -> None:
        with (
            patch.object(core, "load_records", return_value=[{"bad": True}]),
            patch.object(core, "validate_record", side_effect=ValueError("bad rollback")),
        ):
            self.assertEqual(
                ["rollback validation failed: bad rollback"], core.rollback_validation_errors()
            )
        backend = unittest.mock.MagicMock()
        backend.load_session_records.return_value = [{"bad": True}]
        with (
            patch.object(core, "storage_backend", return_value=backend),
            patch.object(core, "validate_session_record", side_effect=ValueError("bad session")),
        ):
            self.assertEqual(
                ["session validation failed: bad session"], core.session_validation_errors()
            )
        conflicting = [
            {"directive_id": "UD-1", "lifecycle": "active"},
            {"directive_id": "UD-2", "lifecycle": "active"},
        ]
        with (
            patch.object(core, "latest_directives", return_value=conflicting),
            patch.object(core, "validate_directive"),
            patch.object(core, "conflicting_directives", return_value=[conflicting[1]]),
        ):
            self.assertEqual(
                ["directive validation failed: active conflict requires guidance AR-0053: UD-2"],
                core.directive_validation_errors(),
            )
        with (
            patch.object(core, "replication_enabled", return_value=True),
            patch.object(core, "replica_heads", return_value=("a" * 40, "b" * 40)),
            patch.object(core, "is_ancestor", return_value=False),
            patch.object(core, "record_replica_block") as blocked,
            self.assertRaisesRegex(core.ReplicaDivergedError, "REPLICA_DIVERGED"),
        ):
            core.sync_replica_before_write()
        blocked.assert_called_once_with("REPLICA_DIVERGED", "a" * 40, "b" * 40)

    def test_lifecycle_fences_reject_stale_or_ambiguous_inputs(self) -> None:
        meta: dict[str, Any] = {"task_revision": 2, "status": "blocked"}
        with (
            patch.object(core, "_session_for_reference", return_value={"task_revision": 1}),
            self.assertRaisesRegex(RuntimeError, "session snapshot revision"),
        ):
            core.apply_resume(
                argparse.Namespace(
                    task="AR-0001", expected_revision=2, session="S-1", note="resume"
                ),
                meta,
                [],
            )
        with patch.object(core, "storage_backend") as backend:
            backend.return_value.load_session_records.return_value = [
                {"task_revision": 2},
                {"task_revision": 2},
            ]
            with (
                patch.object(core, "validate_session_record"),
                self.assertRaisesRegex(RuntimeError, "blocked provenance is ambiguous"),
            ):
                core._blocked_provenance("AR-0001", 2)
        expired = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
        with self.assertRaisesRegex(RuntimeError, "does not have an active claim"):
            core.apply_recover_expired(
                argparse.Namespace(task="AR-0001", expected_revision=2, note="recover"),
                {"task_revision": 2, "status": "open", "claim_expires": expired.isoformat()},
                [],
            )
        with self.assertRaisesRegex(RuntimeError, "invalid claim expiry"):
            core.apply_recover_expired(
                argparse.Namespace(task="AR-0001", expected_revision=2, note="recover"),
                {
                    "task_revision": 2,
                    "status": "in_progress",
                    "owner": "worker-a",
                    "claim_expires": "invalid",
                },
                [],
            )

    def test_read_only_observation_and_dispatch_fail_closed(self) -> None:
        with (
            patch.object(
                core,
                "config",
                return_value={
                    "projects_root": str(self.root),
                    "product_worktree": "product",
                    "github_repository": "owner/product",
                },
            ),
            patch.object(
                core,
                "run_github_observation",
                return_value=subprocess.CompletedProcess([], 0, "[]", ""),
            ),
            patch.object(core, "run", return_value=subprocess.CompletedProcess([], 0, "", "")),
            self.assertRaisesRegex(RuntimeError, "remote main is missing"),
        ):
            core.project_scan()
        with patch.object(core, "backend_selection", return_value={"backend": "git"}):
            with self.assertRaisesRegex(RuntimeError, "board requires the SQLite"):
                core.cmd_board()
            with self.assertRaisesRegex(RuntimeError, "metrics requires the SQLite"):
                core.cmd_metrics()
        with patch.object(core, "dispatch_fast_command", return_value=17) as dispatched:
            self.assertEqual(17, core.dispatch_bound_command(argparse.Namespace(cmd="fast")))
        dispatched.assert_called_once()


if __name__ == "__main__":
    unittest.main()
