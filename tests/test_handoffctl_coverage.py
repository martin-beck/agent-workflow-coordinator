# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Broader handoffctl contract coverage, owned separately from change-drift."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
import subprocess
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from tools import change_drift, execution_receipt, handoffctl


def git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603
        ["/usr/bin/git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
    )


class HandoffctlCoverageTests(unittest.TestCase):
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

    def test_handoffctl_claim_identity_and_global_identity_fail_closed(self) -> None:
        invalid = argparse.Namespace(worker_pid=0, worker_start_time=1)
        with self.assertRaisesRegex(RuntimeError, "claim worker identity"):
            handoffctl.claimed_process_identity(invalid)

        stale = argparse.Namespace(worker_pid=7, worker_start_time=4)
        with (
            patch.object(handoffctl, "process_start_time", return_value=3),
            self.assertRaisesRegex(RuntimeError, "live matching process"),
        ):
            handoffctl.claimed_process_identity(stale)

        expired = {
            "id": "AR-0001",
            "status": "in_progress",
            "owner": "",
            "claim_expires": "2000-01-01T00:00:00+00:00",
        }
        errors = handoffctl.claim_errors(expired, {}, {}, {})
        self.assertIn("AR-0001: active without claim", errors)

        first = {**expired, "owner": "worker", "worktree_key": "tree", "branch": "branch"}
        second = {**first, "id": "AR-0002"}
        errors = handoffctl.claim_errors(
            second, {"worker": "AR-0001"}, {"tree": "AR-0001"}, {"branch": "AR-0001"}
        )
        self.assertEqual(3, len([error for error in errors if "also used" in error]))
        tasks = [(Path("one.md"), first, ""), (Path("two.md"), second, "")]
        with (
            patch.object(handoffctl, "graph_errors", return_value=[]),
            patch.object(handoffctl, "hierarchy_errors", return_value=[]),
            patch.object(handoffctl, "supersession_errors", return_value=[]),
        ):
            errors = handoffctl.mutation_global_errors(tasks)
        self.assertIn("AR-0002: active owner also used by AR-0001", errors)

    def test_handoffctl_validation_journals_and_stale_projection_fail_closed(self) -> None:
        with patch.object(handoffctl, "load_records", side_effect=OSError("bad journal")):
            self.assertEqual(
                ["rollback validation failed: bad journal"], handoffctl.rollback_validation_errors()
            )

        backend = unittest.mock.Mock()
        backend.load_session_records.side_effect = ValueError("bad session")
        with patch.object(handoffctl, "storage_backend", return_value=backend):
            self.assertEqual(
                ["session validation failed: bad session"], handoffctl.session_validation_errors()
            )

        directive = {"directive_id": "UD-0001", "lifecycle": "active"}
        with (
            patch.object(handoffctl, "latest_directives", return_value=[directive]),
            patch.object(handoffctl, "validate_directive"),
            patch.object(
                handoffctl, "conflicting_directives", return_value=[{"directive_id": "UD-0002"}]
            ),
        ):
            errors = handoffctl.directive_validation_errors()
        self.assertEqual(1, len(errors))
        self.assertIn("guidance AR-0053", errors[0])

        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoints = root / "checkpoints"
            checkpoints.mkdir()
            stale = checkpoints / "AR-9999.jsonl"
            stale.write_text("obsolete\n", encoding="utf-8")
            with patch.object(handoffctl, "ROOT", root):
                self.assertEqual([], handoffctl.write_checkpoint_projections([]))
            self.assertFalse(stale.exists())

    def test_handoffctl_replication_and_reconcile_errors_are_not_silenced(self) -> None:
        bad_oid = subprocess.CompletedProcess(["git"], 0, stdout="not-an-oid\n")
        with (
            patch.object(handoffctl, "run", return_value=bad_oid),
            self.assertRaisesRegex(RuntimeError, "cannot identify signed receipt"),
        ):
            handoffctl.current_commit_oid()

        origin = subprocess.CompletedProcess(["git"], 0, stdout="origin\n")
        detached = subprocess.CompletedProcess(["git"], 0, stdout="\n")
        with (
            patch.object(handoffctl, "run", side_effect=[origin, detached]),
            self.assertRaisesRegex(RuntimeError, "requires the main checkout"),
        ):
            handoffctl.replica_heads()

        with (
            patch.object(handoffctl, "replication_enabled", return_value=True),
            patch.object(handoffctl, "replica_heads", return_value=("a", "b")),
            patch.object(handoffctl, "is_ancestor", return_value=False),
            patch.object(handoffctl, "record_replica_block") as record,
            self.assertRaisesRegex(handoffctl.ReplicaDivergedError, "REPLICA_DIVERGED"),
        ):
            handoffctl.sync_replica_before_write()
        record.assert_called_once_with("REPLICA_DIVERGED", "a", "b")

        with (
            patch.object(handoffctl, "policy_snapshot", return_value=object()),
            patch.object(handoffctl, "backend_selection", return_value={"backend": "git"}),
            self.assertRaisesRegex(RuntimeError, "PUSH_REQUIRES_COMMIT"),
        ):
            handoffctl.reconcile(do_commit=False, push=True)

    def test_handoffctl_pending_and_dependency_records_reject_hostile_shapes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / ".runtime"
            runtime.mkdir()
            marker = runtime / "last-reconcile.json"
            marker.write_text('{"pending_paths":"not-a-list"}', encoding="utf-8")
            with (
                patch.object(handoffctl, "ROOT", root),
                patch.object(handoffctl, "RUNTIME", runtime),
                self.assertRaisesRegex(RuntimeError, "invalid pending reconciliation paths"),
            ):
                handoffctl.pending_observation_paths()

        done = [(Path("done.md"), {"id": "AR-0001", "status": "done"}, "")]
        self.assertTrue(handoffctl.dependency_satisfied("AR-0001", done))
        self.assertFalse(handoffctl.dependency_satisfied("AR-9999", done))
        malformed = [
            (Path("bad.md"), {"id": "AR-0002", "status": "superseded", "superseded_by": "bad"}, "")
        ]
        self.assertFalse(handoffctl.dependency_satisfied("AR-0002", malformed))

    def test_handoffctl_lifecycle_and_execution_boundaries_reject_stale_state(self) -> None:
        resume = argparse.Namespace(
            task="AR-0001", expected_revision=4, session="AR-0001@4", note="resume"
        )
        blocked = {"task_revision": 4, "status": "blocked", "owner": "", "claim_expires": ""}
        with (
            patch.object(handoffctl, "_session_for_reference", return_value={"task_revision": 3}),
            self.assertRaisesRegex(RuntimeError, "session snapshot revision"),
        ):
            handoffctl.apply_resume(resume, blocked, [])

        unblock = argparse.Namespace(task="AR-0001", expected_revision=4, note="unblock")
        with self.assertRaisesRegex(RuntimeError, "is not blocked"):
            handoffctl.apply_unblock(unblock, {"task_revision": 4, "status": "open"}, [])

        recover = argparse.Namespace(task="AR-0001", expected_revision=4, note="recover")
        with self.assertRaisesRegex(RuntimeError, "does not have an active claim"):
            handoffctl.apply_recover_expired(recover, {"task_revision": 4, "status": "open"}, [])

        reservation = {"execution_id": "execution-1", "owner": "worker"}
        finish = argparse.Namespace(execution_id="execution-1", owner="worker", note="finish")
        with (
            patch.object(handoffctl, "execution_result_matches", return_value=False),
            self.assertRaisesRegex(RuntimeError, "exact durable result"),
        ):
            handoffctl.apply_execution_finish(finish, {"execution_reservation": reservation})

        result = argparse.Namespace(execution_id="execution-1")
        with (
            patch.object(execution_receipt, "build", side_effect=ValueError("bad receipt")),
            self.assertRaisesRegex(RuntimeError, "bad receipt"),
        ):
            handoffctl.apply_execution_result(result, {"execution_reservation": reservation})

    def test_handoffctl_lock_and_metadata_integrity_paths_fail_closed(self) -> None:
        with (
            TemporaryDirectory() as directory,
        ):
            path = Path(directory) / "execution.lock"
            with (
                patch.object(handoffctl, "task_execution_lock_path", return_value=path),
                patch.object(
                    fcntl, "flock", side_effect=[BlockingIOError(), BlockingIOError(), None]
                ),
                patch.object(time, "monotonic", side_effect=[0.0, 0.25, 1.0]),
                patch.object(time, "sleep") as sleep,
                self.assertRaisesRegex(handoffctl.LockTimeoutError, "LOCK_TIMEOUT"),
                handoffctl.task_execution_locked("AR-0001", exclusive=True, timeout=0.5),
            ):
                self.fail("a timed-out lock must not yield")
        sleep.assert_called_once()

        malformed = {
            "claim_process": {},
            "execution_reservation": {},
            "execution_result": {},
            "change_drift": "not-a-list",
        }
        errors = handoffctl.execution_metadata_errors("AR-0001", malformed)
        self.assertEqual(4, len(errors))
        self.assertTrue(any("change-drift" in error for error in errors))

        boot = Mock()
        boot.read_text.return_value = "not-a-uuid"
        namespace = Mock()
        namespace.stat.return_value = argparse.Namespace(st_dev=1, st_ino=2)
        with (
            patch.object(handoffctl, "Path", side_effect=[boot, namespace]),
            self.assertRaisesRegex(RuntimeError, "invalid boot identity"),
        ):
            handoffctl.worker_execution_scope()
        with (
            patch.object(handoffctl, "Path", side_effect=OSError("no proc")),
            self.assertRaisesRegex(
                RuntimeError, "cannot independently read worker execution scope"
            ),
        ):
            handoffctl.worker_execution_scope()

        self.assertFalse(
            handoffctl.legacy_recovery_admitted(
                argparse.Namespace(allow_legacy_without_identity=True),
                {"claim_process": {}, "claim_expires": "2000-01-01T00:00:00+00:00"},
            )
        )

    def test_handoffctl_lifecycle_rejections_preserve_exact_fences(self) -> None:
        baseline = change_drift.baseline("AR-0001", 2, "now", self.root, ["src"])
        with (
            patch.object(handoffctl, "done_admission_error", return_value=None),
            self.assertRaisesRegex(RuntimeError, "DELTA_REQUIRED"),
        ):
            handoffctl.require_done_admission(
                {"id": "AR-0001", "status": "in_progress", "change_drift": [baseline]}
            )

        duplicate = {"id": "AR-0001", "status": "open"}
        with (
            patch.object(handoffctl, "graph_errors", return_value=[]),
            patch.object(handoffctl, "hierarchy_errors", return_value=[]),
            patch.object(handoffctl, "supersession_errors", return_value=[]),
        ):
            errors = handoffctl.mutation_global_errors(
                [(Path("one.md"), duplicate, ""), (Path("two.md"), duplicate, "")]
            )
        self.assertIn("duplicate AR-0001", errors)

        with (
            patch.object(handoffctl, "load_records", return_value=[{}]),
            patch.object(handoffctl, "validate_record", side_effect=ValueError("bad record")),
        ):
            self.assertEqual(
                ["rollback validation failed: bad record"], handoffctl.rollback_validation_errors()
            )
        with patch.object(handoffctl, "latest_directives", side_effect=ValueError("bad directive")):
            self.assertEqual(
                ["directive validation failed: bad directive"],
                handoffctl.directive_validation_errors(),
            )

        backend = Mock()
        backend.load_session_records.return_value = [{"task_revision": 4}]
        with (
            patch.object(handoffctl, "storage_backend", return_value=backend),
            patch.object(handoffctl, "validate_session_record"),
            self.assertRaisesRegex(RuntimeError, "blocked provenance is ambiguous"),
        ):
            handoffctl._blocked_provenance("AR-0001", 4)

        expired = argparse.Namespace(task="AR-0001", expected_revision=4, note="")
        active = {
            "task_revision": 4,
            "status": "in_progress",
            "owner": "worker",
            "claim_expires": "2000-01-01T00:00:00+00:00",
        }
        with self.assertRaisesRegex(RuntimeError, "recovery note"):
            handoffctl.apply_recover_expired(expired, active, [])

    def test_handoffctl_acceptance_binding_checkpoint_and_gate_errors_are_explicit(self) -> None:
        accept = argparse.Namespace(task="AR-0001", owner="worker", expected_revision=4)
        with self.assertRaisesRegex(RuntimeError, "accept requires an active task"):
            handoffctl.apply_accept(
                accept, {"owner": "worker", "status": "open", "task_revision": 4}, None
            )

        bind = argparse.Namespace(task="AR-0001", owner="worker", expected_revision=4)
        with self.assertRaisesRegex(RuntimeError, "bind-spec requires an active task"):
            handoffctl.apply_bind_spec(
                bind, {"owner": "worker", "status": "open", "task_revision": 4}, None
            )
        with self.assertRaisesRegex(RuntimeError, "unexpired owner lease"):
            handoffctl.apply_bind_spec(
                bind,
                {
                    "owner": "worker",
                    "status": "in_progress",
                    "task_revision": 4,
                    "claim_expires": "not-a-date",
                },
                None,
            )

        with patch.object(handoffctl, "config", return_value={}):
            self.assertFalse(handoffctl.valid_binding_worktree("tree"))
        malformed: object
        for malformed in (None, "", 7, []):
            with (
                self.subTest(projects_root=malformed),
                patch.object(handoffctl, "config", return_value={"projects_root": malformed}),
            ):
                self.assertFalse(handoffctl.valid_binding_worktree("tree"))

        checkpoint = argparse.Namespace(task="AR-0001", owner="worker", expected_revision=4)
        with self.assertRaisesRegex(RuntimeError, "owned by nobody"):
            handoffctl.apply_checkpoint(checkpoint, {"owner": "", "task_revision": 4})
        with self.assertRaisesRegex(RuntimeError, "stale revision"):
            handoffctl.apply_checkpoint(checkpoint, {"owner": "worker", "task_revision": 3})

        gate = argparse.Namespace(action="resolve", session_id="session")
        with self.assertRaisesRegex(RuntimeError, "only when opening a gate"):
            handoffctl._human_session_values(gate, "now")

        result = argparse.Namespace(execution_id="execution-1")
        reservation = {"execution_id": "execution-1", "owner": "worker"}
        with (
            patch.object(execution_receipt, "build", return_value={"result": "same"}),
            self.assertRaises(handoffctl.IdempotentMutationError),
        ):
            handoffctl.apply_execution_result(
                result,
                {"execution_reservation": reservation, "execution_result": {"result": "same"}},
            )

    def test_handoffctl_dispatch_and_upgrade_package_paths_remain_explicit(self) -> None:
        with (
            patch.object(handoffctl, "backend_selection", return_value={"backend": "git"}),
            self.assertRaisesRegex(RuntimeError, "metrics requires the SQLite"),
        ):
            handoffctl.cmd_metrics()

        checkpoint = argparse.Namespace()
        head = subprocess.CompletedProcess(["git"], 0, stdout="a" * 40 + "\n")
        with (
            patch.object(handoffctl, "invocation_worktree", return_value=None),
            patch.object(handoffctl, "run", return_value=head) as run,
            patch.object(handoffctl, "mutate") as mutate,
        ):
            handoffctl.cmd_checkpoint(checkpoint)
        self.assertIn("-C", run.call_args.args[0])
        mutate.assert_called_once_with(checkpoint, "checkpoint")

        with (
            patch.object(handoffctl, "_directive_lifecycle", wraps=handoffctl._directive_lifecycle),
            self.assertRaisesRegex(RuntimeError, "requires guidance AR-0053"),
        ):
            handoffctl._directive_lifecycle("escalate", {"guidance_ref": "AR-0001"}, [])

        from tools import upgrade_commands

        upgrade = argparse.Namespace(upgrade_action="check", contract="contract.json", binding=None)
        with patch.object(upgrade_commands, "execute_upgrade_command", return_value=19) as execute:
            self.assertEqual(19, handoffctl.cmd_upgrade(upgrade))
        execute.assert_called_once()

        from tools import fast_receipt_cli

        fast = argparse.Namespace(cmd="fast")
        with patch.object(fast_receipt_cli, "dispatch_fast", return_value=23) as dispatch:
            self.assertEqual(23, handoffctl.dispatch_bound_command(fast))
        dispatch.assert_called_once_with(handoffctl, fast)

    def test_handoffctl_observation_marker_and_projection_errors_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / ".runtime"
            runtime.mkdir()
            marker = runtime / "last-reconcile.json"
            marker.write_text(
                '{"pending_paths":[],"pending_hashes":{"extra":"x"}}', encoding="utf-8"
            )
            with (
                patch.object(handoffctl, "ROOT", root),
                patch.object(handoffctl, "RUNTIME", runtime),
                self.assertRaisesRegex(RuntimeError, "invalid pending reconciliation hashes"),
            ):
                handoffctl.pending_observation_paths()
            with (
                patch.object(handoffctl, "ROOT", root),
                patch.object(handoffctl, "all_tasks", return_value=[]),
                patch.object(handoffctl, "generated_paths", return_value=[]),
                self.assertRaisesRegex(RuntimeError, "outside coordinator projections"),
            ):
                handoffctl.validate_pending_path_scope([root / "hostile.txt"])

        with (
            patch.object(handoffctl, "atomic"),
            patch.object(handoffctl, "render_current", return_value="current"),
            patch.object(handoffctl, "project_settings", return_value={"status_view": True}),
            patch.object(handoffctl, "render_status_views", return_value={"STATUS.md": "status"}),
            patch.object(handoffctl, "write_status_views") as write_status,
            patch.object(handoffctl, "live_docs", return_value=("project", "worktrees")),
        ):
            handoffctl.write_generated_views([], {})
        write_status.assert_called_once_with({"STATUS.md": "status"})

        with (
            patch.object(handoffctl, "policy_snapshot", return_value=object()),
            patch.object(handoffctl, "backend_selection", return_value={"backend": "git"}),
            patch.object(handoffctl, "config", return_value={}),
            patch.object(handoffctl, "project_binding", return_value={}),
            patch.object(handoffctl, "reserve_observation_ticket", return_value=1),
            patch.object(handoffctl, "project_scan", return_value={}),
            patch.object(handoffctl, "locked", return_value=contextlib.nullcontext()),
            patch.object(handoffctl, "apply_git_reconciliation", return_value=None),
            self.assertRaisesRegex(RuntimeError, "OBSERVATION_SUPERSEDED"),
        ):
            handoffctl.reconcile(do_commit=True)

    def test_handoffctl_live_read_retries_and_refuses_inconsistent_snapshots(self) -> None:
        with (
            patch.object(handoffctl, "published_observation_ticket", return_value=1),
            patch.object(handoffctl, "project_binding", return_value={"project": "one"}),
            patch.object(handoffctl, "project_scan", return_value={"scan": "one"}),
            patch.object(handoffctl, "locked", return_value=contextlib.nullcontext()),
            patch.object(handoffctl, "backend_selection", return_value={"backend": "sqlite"}),
            self.assertRaisesRegex(RuntimeError, "BACKEND_CHANGED: retry doctor"),
        ):
            handoffctl.git_doctor_checks(live=True)

        with (
            patch.object(
                handoffctl, "published_observation_ticket", side_effect=[1, 2, 1, 2, 1, 2]
            ),
            patch.object(handoffctl, "project_scan", return_value={"scan": "one"}),
            patch.object(handoffctl, "locked", return_value=contextlib.nullcontext()),
            patch.object(handoffctl, "backend_selection", return_value={"backend": "git"}),
            patch.object(handoffctl, "project_binding", return_value={"project": "one"}),
            patch.object(handoffctl, "doctor_checks", return_value=["PROJECT_STATE.md is stale"]),
        ):
            self.assertEqual(
                ["OBSERVATION_CHANGED: retry doctor"], handoffctl.git_doctor_checks(live=True)
            )

        with (
            patch.object(
                handoffctl,
                "backend_selection",
                side_effect=[{"backend": "git"}, {"backend": "sqlite"}],
            ),
            patch.object(handoffctl, "published_observation_ticket", return_value=1),
            patch.object(handoffctl, "project_scan", return_value={}),
            patch.object(handoffctl, "locked", return_value=contextlib.nullcontext()),
            self.assertRaisesRegex(RuntimeError, "BACKEND_CHANGED: retry snapshot"),
        ):
            handoffctl.cmd_snapshot()

        with (
            patch.object(handoffctl, "backend_selection", return_value={"backend": "git"}),
            patch.object(
                handoffctl, "published_observation_ticket", side_effect=[1, 2, 1, 2, 1, 2]
            ),
            patch.object(handoffctl, "project_scan", return_value={}),
            patch.object(handoffctl, "locked", return_value=contextlib.nullcontext()),
            self.assertRaisesRegex(RuntimeError, "OBSERVATION_CHANGED: retry snapshot"),
        ):
            handoffctl.cmd_snapshot()

    def test_handoffctl_mutation_and_rollback_adapters_preserve_failure_causes(self) -> None:
        settings = {"project_name": "stable"}
        with (
            patch.object(handoffctl, "project_settings", return_value={"project_name": "changed"}),
            self.assertRaisesRegex(RuntimeError, "PROJECT_CONFIG_CHANGED"),
        ):
            handoffctl.require_project_settings_unchanged(settings)

        commit_args = argparse.Namespace(task="AR-0001")
        with (
            patch.object(handoffctl, "commit", return_value=False),
            patch.object(handoffctl, "changed_paths", return_value=[]),
            self.assertRaisesRegex(RuntimeError, "produced no signed commit"),
        ):
            handoffctl.commit_persisted_mutation(
                commit_args, "claim", "a" * 32, {}, {"task_revision": 4}
            )

        receipt_args = argparse.Namespace(_receipt_id="a" * 32, expected_revision=4)
        with (
            patch.object(handoffctl, "backend_selection", return_value={"backend": "sqlite"}),
            self.assertRaisesRegex(RuntimeError, "fast receipts require Git"),
        ):
            handoffctl._mutate_fenced(receipt_args, "claim")

        product = Path("/product")
        clean = subprocess.CompletedProcess(["git"], 0, stdout="")
        exists = subprocess.CompletedProcess(["git"], 0, stdout="")
        head = subprocess.CompletedProcess(["git"], 0, stdout="b" * 40 + "\n")
        not_ancestor = subprocess.CompletedProcess(["git"], 1, stdout="")
        with (
            patch.object(handoffctl, "run", side_effect=[clean, exists, head, not_ancestor]),
            self.assertRaisesRegex(RuntimeError, "not an ancestor"),
        ):
            handoffctl._product_commit_for_checkpoint(product, "a" * 40)

        previous = {
            "rollback_commit": "",
            "current_commit": "a",
            "operation_id": "op",
            "revision": 1,
        }
        with self.assertRaisesRegex(RuntimeError, "head changed during ambiguous rollback"):
            handoffctl._start_rollback(
                argparse.Namespace(reconcile=True), {}, product, "b", previous
            )

    def test_handoffctl_upgrade_and_main_routes_do_not_fall_through(self) -> None:
        from tools import runtime_bootstrap, upgrade_commands

        consume = argparse.Namespace(
            upgrade_action="consume",
            selector="selector.json",
            releases_root="releases",
            source_commit="a" * 40,
            tag_ref="refs/tags/v0.6.0",
            tag_object="b" * 40,
            signature_sha256="c" * 64,
            trust_policy_sha256="d" * 64,
            vendor_manifest_sha256="e" * 64,
            manifest_digest="f" * 64,
            runtime_command=["python", "-m", "worker"],
        )
        with patch.object(
            upgrade_commands, "consume_selected_runtime_command", return_value=29
        ) as command:
            self.assertEqual(29, handoffctl.cmd_upgrade(consume))
        self.assertIsInstance(command.call_args.args[2], runtime_bootstrap.ExpectedRuntimeIdentity)

        apply = argparse.Namespace(upgrade_action="apply", binding=None, contract="contract.json")
        with self.assertRaisesRegex(RuntimeError, "require --binding"):
            handoffctl.cmd_upgrade(apply)

        migrate = argparse.Namespace(cmd="migrate")
        with patch.object(handoffctl, "cmd_migrate") as command:
            self.assertEqual(0, handoffctl.dispatch_bound_standard_command(migrate))
        command.assert_called_once_with(migrate)

    def test_handoffctl_small_integrity_helpers_cover_real_rejection_boundaries(self) -> None:
        with patch.object(handoffctl, "private_text_errors", return_value=["private"]):
            self.assertEqual(
                ["AR-0001: privacy-unsafe short_title"],
                handoffctl.short_title_errors(Path("task.md"), "AR-0001", "safe words"),
            )
        with patch.object(
            handoffctl,
            "legacy_command_results",
            return_value=[{"task": "AR-0001", "owner": "worker", "argv_sha256": "digest"}],
        ):
            self.assertTrue(
                handoffctl.GitBackend().has_command_result("AR-0001", "worker", "digest")
            )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / ".runtime"
            runtime.mkdir()
            lock = runtime / "state.lock"
            root.joinpath(".git").mkdir()
            with (
                patch.object(handoffctl, "ROOT", root),
                patch.object(handoffctl, "RUNTIME", runtime),
                patch.object(handoffctl, "LOCK", lock),
                patch.object(
                    handoffctl,
                    "run",
                    return_value=subprocess.CompletedProcess(["git"], 1, stdout=""),
                ),
                self.assertRaisesRegex(RuntimeError, "cannot resolve repository-common"),
            ):
                handoffctl.coordinator_lock_path()
            with patch.dict("os.environ", {}, clear=True):
                handoffctl.trace_lock_metric("test", "shared", 0.0, None)

            sequence = runtime / "observation-sequence"
            sequence.write_text("-1\n", encoding="utf-8")
            with (
                patch.object(handoffctl, "RUNTIME", runtime),
                self.assertRaisesRegex(RuntimeError, "invalid observation sequence"),
            ):
                handoffctl.reserve_observation_ticket()
            published = runtime / "observation-published"
            published.write_text("-1\n", encoding="utf-8")
            with (
                patch.object(handoffctl, "RUNTIME", runtime),
                self.assertRaisesRegex(RuntimeError, "invalid published observation ticket"),
            ):
                handoffctl.published_observation_ticket()

            target = root / "target.txt"
            with (
                patch.object(Path, "replace", side_effect=OSError("replace failed")),
                self.assertRaisesRegex(OSError, "replace failed"),
            ):
                handoffctl.atomic(target, "content")
            self.assertFalse(list(root.glob(".target.txt.*")))

        self.assertEqual([], handoffctl.parse_worktree_listing("malformed\n\n"))
        self.assertFalse(handoffctl.valid_claim_process({"pid": 1, "start_time": -1}))
        with patch.object(handoffctl, "valid_execution_envelope_binding", return_value=False):
            self.assertIn(
                "AR-0001: invalid execution envelope binding",
                handoffctl.execution_metadata_errors("AR-0001", {}),
            )

    def test_handoffctl_remaining_lifecycle_and_command_rejections_are_explicit(self) -> None:
        result = argparse.Namespace(execution_id="wanted")
        with self.assertRaisesRegex(RuntimeError, "reservation no longer matches"):
            handoffctl.apply_execution_result(
                result, {"execution_reservation": {"execution_id": "other"}}
            )

        rollback = argparse.Namespace(
            rollback_checkpoint={
                "state": {"id": "AR-0001", "short_title": "Restore title", "status": "open"},
                "source_commit": "a" * 40,
                "name": "checkpoint",
            }
        )
        meta: dict[str, object] = {"id": "AR-0001", "owner": "", "claim_expires": ""}
        self.assertIn("Restored checkpoint", handoffctl.apply_rollback(rollback, meta))
        self.assertEqual("Restore title", meta["short_title"])

        recover = argparse.Namespace()
        with patch.object(
            handoffctl, "apply_recover_execution", return_value="recovered"
        ) as command:
            self.assertEqual(
                "recovered", handoffctl.apply_transition(recover, "recover-execution", {}, [])
            )
        command.assert_called_once_with(recover, {})

        abandon = argparse.Namespace()
        with patch.object(
            handoffctl, "apply_abandon_execution", return_value="abandoned"
        ) as command:
            self.assertEqual(
                "abandoned", handoffctl.apply_transition(abandon, "abandon-execution", {}, [])
            )
        command.assert_called_once_with(abandon, {})

        with (
            patch.object(handoffctl, "published_observation_ticket", return_value=1),
            patch.object(handoffctl, "project_binding", return_value={"project": "one"}),
            patch.object(handoffctl, "project_scan", return_value={"scan": "one"}),
            patch.object(handoffctl, "locked", return_value=contextlib.nullcontext()),
            patch.object(handoffctl, "backend_selection", return_value={"backend": "git"}),
            patch.object(handoffctl, "CONFIG", Path("pyproject.toml")),
            patch.object(handoffctl, "config", side_effect=[{"version": 1}, {"version": 2}]),
            self.assertRaisesRegex(RuntimeError, "OBSERVATION_INPUT_CHANGED: retry doctor"),
        ):
            handoffctl.git_doctor_checks(live=True)

        with (
            patch.object(handoffctl, "backend_selection", return_value={"backend": "git"}),
            patch.object(handoffctl, "published_observation_ticket", return_value=1),
            patch.object(handoffctl, "project_scan", return_value={}),
            patch.object(handoffctl, "locked", return_value=contextlib.nullcontext()),
            patch.object(handoffctl, "validate", return_value=["invalid state"]),
            self.assertRaisesRegex(RuntimeError, "snapshot refused"),
        ):
            handoffctl.cmd_snapshot()

        self.assertEqual(
            "escalated",
            handoffctl._directive_lifecycle("escalate", {"guidance_ref": "AR-0053"}, []),
        )

    def test_handoffctl_initialization_refuses_wrong_root_or_origin(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            args = argparse.Namespace(
                state_repository="owner/state",
                product_repository="owner/product",
                project_name="project",
                project_title="Project",
                status_view=False,
                commit_signoff=False,
                backend="git",
            )
            missing = root / "missing"
            top = subprocess.CompletedProcess(["git"], 0, stdout=str(root) + "\n")
            with (
                patch.object(handoffctl, "ROOT", missing),
                patch.object(handoffctl, "PROJECT_CONFIG", root / "config"),
                patch.object(handoffctl, "BINDING", root / "binding"),
                patch.object(handoffctl, "BACKEND_CONFIG", root / "backend"),
                patch.object(handoffctl, "run", return_value=top),
                self.assertRaisesRegex(RuntimeError, "state repository root"),
            ):
                handoffctl.cmd_init(args)
            with (
                patch.object(handoffctl, "ROOT", root),
                patch.object(handoffctl, "PROJECT_CONFIG", root / "config"),
                patch.object(handoffctl, "BINDING", root / "binding"),
                patch.object(handoffctl, "BACKEND_CONFIG", root / "backend"),
                patch.object(handoffctl, "run", return_value=top),
                patch.object(Path, "cwd", return_value=root),
                patch.object(handoffctl, "git_repository_slug", return_value="other/state"),
                self.assertRaisesRegex(RuntimeError, "origin does not match"),
            ):
                handoffctl.cmd_init(args)

    def test_handoffctl_final_fail_closed_branch_variants(self) -> None:
        active = [
            {"directive_id": "UD-0001", "lifecycle": "active"},
            {"directive_id": "UD-0002", "lifecycle": "active"},
        ]
        with (
            patch.object(handoffctl, "latest_directives", return_value=active),
            patch.object(handoffctl, "validate_directive"),
            patch.object(handoffctl, "conflicting_directives", return_value=[]),
        ):
            self.assertEqual([], handoffctl.directive_validation_errors())

        with (
            patch.object(handoffctl, "atomic"),
            patch.object(handoffctl, "render_current", return_value="current"),
            patch.object(handoffctl, "project_settings", return_value={"status_view": False}),
            patch.object(handoffctl, "live_docs", return_value=("project", "worktrees")),
        ):
            handoffctl.write_generated_views([], {})

        invalid_expiry = argparse.Namespace(task="AR-0001", expected_revision=4, note="repair")
        with self.assertRaisesRegex(RuntimeError, "invalid claim expiry"):
            handoffctl.apply_recover_expired(
                invalid_expiry,
                {
                    "task_revision": 4,
                    "status": "in_progress",
                    "owner": "worker",
                    "claim_expires": "invalid",
                },
                [],
            )

    def test_handoffctl_directive_and_unknown_command_routes_do_not_fall_through(self) -> None:
        with (
            patch.object(handoffctl, "assert_project_binding"),
            patch.object(handoffctl, "cmd_directive") as directive,
            patch("sys.argv", ["handoffctl", "directive", "list"]),
        ):
            self.assertEqual(0, handoffctl.main())
        self.assertEqual("directive", directive.call_args.args[0].cmd)
        self.assertEqual("list", directive.call_args.args[0].directive_action)
        self.assertEqual(
            0, handoffctl.dispatch_bound_standard_command(argparse.Namespace(cmd="unknown"))
        )

    def test_handoffctl_binding_observation_and_projection_races_fail_closed(self) -> None:
        """A non-product checkout or a raced projection must never be trusted."""
        binding = {
            "project_id": "project-id",
            "state_repository": "owner/state",
            "product_repository": "owner/product",
        }
        with TemporaryDirectory() as directory:
            projects = Path(directory)
            product = projects / "product"
            foreign = projects / "foreign"
            product.mkdir()
            foreign.mkdir()
            with (
                patch.object(
                    handoffctl, "project_settings", return_value={"project_id": "project-id"}
                ),
                patch.object(handoffctl, "project_binding", return_value=binding),
                patch.object(handoffctl, "_assert_storage_binding"),
                patch.object(
                    handoffctl, "configured_product_checkout", return_value=(projects, product)
                ),
                patch.object(
                    handoffctl,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        ["git"], 0, stdout=str(handoffctl.ROOT)
                    ),
                ),
                patch.object(handoffctl, "git_repository_slug", return_value="owner/state"),
                self.assertRaisesRegex(RuntimeError, "bound state or product project"),
            ):
                handoffctl.assert_project_binding(foreign)

        candidate = self.root / "CURRENT.md"
        candidate.write_text("current\n", encoding="utf-8")
        candidate.chmod(0o600)
        original_lstat = Path.lstat
        calls = 0

        def vanish_after_open(path: Path) -> os.stat_result:
            nonlocal calls
            calls += 1
            if path == candidate and calls > 1:
                raise OSError("projection disappeared")
            return original_lstat(path)

        with patch.object(Path, "lstat", autospec=True, side_effect=vanish_after_open):
            self.assertFalse(handoffctl.unchanged_generated_view(candidate, "current\n"))

    def test_handoffctl_remote_and_execution_journal_failures_remain_visible(self) -> None:
        """Do not convert a missing main ref or unjournaled command into success."""
        settings = {
            "projects_root": str(self.root.parent),
            "product_worktree": self.root.name,
            "github_repository": "owner/product",
        }
        listing = subprocess.CompletedProcess(
            ["git"], 0, stdout="worktree /tmp/none\nHEAD a\nbranch refs/heads/main\n\n"
        )
        no_remote = subprocess.CompletedProcess(["git"], 0, stdout="")
        with (
            patch.object(handoffctl, "config", return_value=settings),
            patch.object(handoffctl, "run", side_effect=[listing, listing, no_remote]),
            patch.object(
                handoffctl,
                "run_github_observation",
                return_value=subprocess.CompletedProcess(["gh"], 0, stdout="[]"),
            ),
            self.assertRaisesRegex(RuntimeError, "remote main is missing"),
        ):
            handoffctl.project_scan()

        args = argparse.Namespace(task="AR-0001", owner="worker", command=["true"], timeout=1.0)
        with (
            patch.object(
                handoffctl,
                "_cmd_run_fenced",
                return_value=({}, "a" * 64, 1.0, False, 0, None, 4),
            ),
            patch.object(
                handoffctl, "append_command_result", side_effect=OSError("journal unavailable")
            ),
            self.assertRaisesRegex(OSError, "journal unavailable"),
        ):
            handoffctl.cmd_run(args)

    def test_invalid_run_timeout_is_rejected_before_envelope_lookup(self) -> None:
        args = argparse.Namespace(task="AR-0001", owner="worker", timeout_seconds=0, command=[])
        with (
            patch.object(handoffctl, "backend_selection", return_value={"backend": "sqlite"}),
            patch.object(handoffctl, "require_active_owner", return_value=object()),
            patch.object(
                handoffctl,
                "run_envelope_timeout",
                side_effect=AssertionError("binding lookup must not occur"),
            ),
            self.assertRaisesRegex(RuntimeError, "timeout must be positive"),
        ):
            handoffctl._cmd_run_fenced(args)
