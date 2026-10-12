# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Execution-envelope lifecycle tests using the canonical handoff fixture."""

from __future__ import annotations

import argparse
import json
import subprocess
import unittest
from typing import cast
from unittest.mock import patch

from test_handoffctl import CORE, HandoffTest


class ExecutionEnvelopeLifecycleTest(HandoffTest):
    """Keep envelope lifecycle coverage outside the vendor-size-capped module."""

    def test_execution_reservation_is_exact_and_blocks_lifecycle_mutation(self) -> None:
        """A second worker cannot reuse or invalidate a bound command fence."""
        meta: dict[str, object] = {
            "id": "AR-0001",
            "owner": "worker-a",
            "status": "in_progress",
            "claim_expires": "2099-01-01T00:00:00+00:00",
            "task_revision": 5,
            "execution_envelope": {},
            "execution_envelope_digest": "sha256:" + "a" * 64,
        }
        start = argparse.Namespace(
            task="AR-0001",
            owner="worker-a",
            expected_revision=5,
            execution_id="b" * 32,
            execution_expires_at="2099-01-01T00:00:00+00:00",
            command_class="test",
            scope_digest="sha256:" + "c" * 64,
            argv_digest="d" * 64,
        )
        self.assertIn("Reserved", CORE.apply_execution_start(start, meta))
        self.assertTrue(CORE.valid_execution_reservation(meta["execution_reservation"]))
        with self.assertRaisesRegex(RuntimeError, "EXECUTION_IN_PROGRESS"):
            CORE.apply_execution_start(start, meta)
        heartbeat = argparse.Namespace(task="AR-0001", owner="worker-a", lease_minutes=10)
        with self.assertRaisesRegex(RuntimeError, "EXECUTION_IN_PROGRESS"):
            CORE.apply_owned_change(heartbeat, "heartbeat", meta)
        pause = argparse.Namespace(
            task="AR-0001", owner="worker-a", expected_revision=5, note="pause"
        )
        with self.assertRaisesRegex(RuntimeError, "EXECUTION_RECOVERY_REQUIRED"):
            CORE.apply_pause(pause, meta)
        finish = argparse.Namespace(
            task="AR-0001",
            owner="worker-a",
            expected_revision=5,
            execution_id="b" * 32,
            note="done",
        )
        self.assertEqual("done", CORE.apply_execution_finish(finish, meta))
        self.assertNotIn("execution_reservation", meta)
        with self.assertRaisesRegex(RuntimeError, "no longer matches"):
            CORE.apply_execution_finish(finish, meta)
        self.assertIn("Reserved", CORE.apply_execution_start(start, meta))
        reservation = cast(dict[str, object], meta["execution_reservation"])
        reservation["expires_at"] = "2000-01-01T00:00:00+00:00"
        recover = argparse.Namespace(task="AR-0001", expected_revision=5, note="worker exited")
        with (
            patch.object(CORE, "require_claimed_worker_absent") as absent,
            self.assertRaisesRegex(RuntimeError, "EXECUTION_RESULT_UNKNOWN"),
        ):
            CORE.apply_recover_execution(recover, meta)
        absent.assert_called_once_with(meta)
        self.assertIs(meta["execution_reservation"], reservation)
        with (
            patch.object(CORE, "require_claimed_worker_absent"),
            self.assertRaisesRegex(RuntimeError, "EXECUTION_RESULT_UNKNOWN"),
        ):
            CORE.apply_recover_execution(recover, meta)
        self.assertIs(meta["execution_reservation"], reservation)

        valid = {
            "execution_id": "a" * 32,
            "owner": "worker-a",
            "task_revision": 1,
            "envelope_digest": "sha256:" + "b" * 64,
            "command_class": "test",
            "scope_digest": "sha256:" + "c" * 64,
            "argv_digest": "d" * 64,
            "expires_at": "2099-01-01T00:00:00+00:00",
        }
        self.assertTrue(CORE.valid_execution_reservation(valid))
        for field, invalid in (
            ("execution_id", "bad"),
            ("owner", ""),
            ("task_revision", 0),
            ("envelope_digest", "bad"),
            ("command_class", "unknown"),
            ("scope_digest", "bad"),
            ("argv_digest", "bad"),
            ("expires_at", "bad"),
            ("expires_at", "2099-01-01T00:00:00"),
        ):
            candidate = {**valid, field: invalid}
            self.assertFalse(CORE.valid_execution_reservation(candidate), field)

    def test_execution_reservation_rejects_invalid_admission_and_recovery(self) -> None:
        """Reservation errors remain fail-closed before a command can be admitted."""
        meta: dict[str, object] = {
            "id": "AR-0001",
            "owner": "worker-a",
            "status": "in_progress",
            "claim_expires": "2099-01-01T00:00:00+00:00",
            "task_revision": 5,
            "execution_envelope_digest": "sha256:" + "a" * 64,
        }
        args = argparse.Namespace(
            task="AR-0001",
            owner="worker-a",
            expected_revision=5,
            execution_id="b" * 32,
            execution_expires_at="2099-01-01T00:00:00+00:00",
            command_class="test",
            scope_digest="sha256:" + "c" * 64,
            argv_digest="d" * 64,
        )
        with self.assertRaisesRegex(RuntimeError, "requires a bound"):
            CORE.apply_execution_start(args, meta)
        meta["execution_envelope"] = {}
        for field, value, message in (
            ("execution_id", "bad", "identifier"),
            ("execution_expires_at", "bad", "expiry"),
            ("execution_expires_at", "2000-01-01T00:00:00+00:00", "future"),
        ):
            candidate = argparse.Namespace(**vars(args))
            setattr(candidate, field, value)
            with self.assertRaisesRegex(RuntimeError, message):
                CORE.apply_execution_start(candidate, dict(meta))
        CORE.apply_execution_start(args, meta)
        finish = argparse.Namespace(
            task="AR-0001",
            owner="worker-b",
            expected_revision=5,
            execution_id="b" * 32,
            note="done",
        )
        with self.assertRaisesRegex(RuntimeError, "owner does not match"):
            CORE.apply_execution_finish(finish, meta)
        finish.owner = "worker-a"
        finish.expected_revision = 4
        with self.assertRaisesRegex(RuntimeError, "stale revision"):
            CORE.apply_execution_finish(finish, meta)
        recover = argparse.Namespace(task="AR-0001", expected_revision=5, note="recover")
        with self.assertRaisesRegex(RuntimeError, "has not expired"):
            CORE.apply_recover_execution(recover, meta)
        meta["execution_reservation"] = {"bad": "record"}
        with self.assertRaisesRegex(RuntimeError, "requires a valid reservation"):
            CORE.apply_recover_execution(recover, meta)

        meta["execution_reservation"] = {
            "execution_id": "a" * 32,
            "owner": "worker-a",
            "task_revision": 5,
            "envelope_digest": "sha256:" + "a" * 64,
            "command_class": "test",
            "scope_digest": "sha256:" + "c" * 64,
            "argv_digest": "d" * 64,
            "expires_at": "2000-01-01T00:00:00+00:00",
        }
        with self.assertRaisesRegex(RuntimeError, "expired execution reservation"):
            CORE.apply_execution_start(args, meta)
        meta.pop("execution_reservation")
        meta["claim_expires"] = "2098-01-01T00:00:00+00:00"
        CORE.apply_execution_start(args, meta)
        self.assertEqual("2099-01-01T00:00:00+00:00", meta["claim_expires"])

    def test_sqlite_recovery_requires_exact_durable_result_evidence(self) -> None:
        """SQLite recovery delegates its identity proof to the durable backend."""
        reservation = {
            "execution_id": "a" * 32,
            "owner": "worker-a",
            "task_revision": 5,
            "envelope_digest": "sha256:" + "b" * 64,
            "command_class": "test",
            "scope_digest": "sha256:" + "c" * 64,
            "argv_digest": "d" * 64,
            "expires_at": "2000-01-01T00:00:00+00:00",
        }
        recover = argparse.Namespace(task="AR-0001", expected_revision=5, note="recover")
        meta: dict[str, object] = {"task_revision": 5, "execution_reservation": reservation}
        with (
            patch.object(CORE, "require_claimed_worker_absent"),
            self.assertRaisesRegex(RuntimeError, "EXECUTION_RESULT_UNKNOWN"),
        ):
            CORE.apply_recover_execution(recover, meta)
        with (
            patch.object(CORE, "require_claimed_worker_absent"),
            self.assertRaisesRegex(RuntimeError, "EXECUTION_RESULT_UNKNOWN"),
        ):
            CORE.apply_recover_execution(recover, meta)
        self.assertIn("execution_reservation", meta)

    def test_bound_envelope_validation_and_binding_errors(self) -> None:
        """Persisted envelope coherence and binding retries reject malformed state."""
        self.make_task()
        path, meta, _body = CORE.locate("AR-0001")
        meta["claim_process"] = {"pid": 0, "start_time": 1}
        meta["execution_reservation"] = {"bad": "record"}
        meta.update(
            execution_envelope_ref="envelopes/AR-0001-e1.json",
            execution_envelope_revision=1,
            execution_envelope_task_revision=1,
            execution_envelope_digest="sha256:" + "a" * 64,
            execution_envelope={},
        )
        errors = CORE.value_errors(path, meta)
        self.assertIn("AR-0001: invalid claim_process", errors)
        self.assertIn("AR-0001: invalid execution_reservation", errors)
        self.assertIn("envelope missing field: envelope_id", CORE.reference_errors(path, meta)[0])

        envelope = {
            "schema_version": 1,
            "envelope_id": "AR-0001-e1",
            "envelope_revision": 1,
            "task": "AR-0001",
            "task_revision": 1,
            "spec_ref": "spec.json",
            "spec_revision": 1,
            "outputs": [{"id": "output", "description": "bounded output"}],
            "non_goals": ["no expansion"],
            "path_scope": {"read": ["src"], "write": ["src"]},
            "command_classes": {"test": {"allowed": True}},
            "evidence": [{"id": "tests", "class": "contract-test", "required": True}],
            "stop_conditions": [{"id": "stop", "kind": "deny"}],
        }
        meta["execution_envelope"] = envelope
        meta["execution_envelope_digest"] = "sha256:" + "0" * 64
        self.assertIn("execution envelope digest mismatch", CORE.reference_errors(path, meta)[0])
        meta["execution_envelope_digest"] = CORE.execution_envelope.canonical_digest(envelope)
        meta["execution_envelope_revision"] = 2
        self.assertIn("execution envelope revision mismatch", CORE.reference_errors(path, meta)[0])
        meta["execution_envelope_revision"] = 1
        meta["execution_envelope_task_revision"] = 2
        self.assertIn(
            "execution envelope task revision mismatch", CORE.reference_errors(path, meta)[0]
        )

        meta.update(
            status="in_progress",
            owner="worker-a",
            claim_expires="2099-01-01T00:00:00+00:00",
            spec_ref="spec.json",
            spec_revision=1,
            execution_envelope_ref=None,
            execution_envelope_revision=None,
            execution_envelope_task_revision=None,
            execution_envelope_digest=None,
            execution_envelope=None,
            execution_reservation=None,
        )
        bind = argparse.Namespace(
            task="AR-0001",
            owner="worker-a",
            expected_revision=1,
            envelope_ref="envelopes/AR-0001-e1.json",
            envelope_revision=1,
        )
        with patch.object(CORE.execution_envelope, "load", return_value=(envelope, "ignored", [])):
            self.assertIn("Bound execution envelope", CORE.apply_bind_envelope(bind, meta))
            with self.assertRaisesRegex(CORE.IdempotentMutationError, "already bound"):
                CORE.apply_bind_envelope(bind, meta)
        changed = argparse.Namespace(**vars(bind))
        changed.envelope_revision = 2
        replacement = {**envelope, "envelope_revision": 2}
        with (
            patch.object(
                CORE.execution_envelope, "load", return_value=(replacement, "ignored", [])
            ),
            self.assertRaisesRegex(RuntimeError, "requires an unbound"),
        ):
            CORE.apply_bind_envelope(changed, meta)

    def test_bind_envelope_candidate_rejects_each_identity_mismatch(self) -> None:
        """A mutable file cannot be substituted for the task-bound envelope identity."""
        args = argparse.Namespace(
            task="AR-0001", envelope_ref="envelopes/AR-0001-e1.json", envelope_revision=1
        )
        meta: dict[str, object] = {"spec_ref": "spec.json", "spec_revision": 1, "task_revision": 1}
        envelope = {
            "schema_version": 1,
            "envelope_id": "AR-0001-e1",
            "envelope_revision": 1,
            "task": "AR-0001",
            "task_revision": 1,
            "spec_ref": "spec.json",
            "spec_revision": 1,
            "outputs": [{"id": "output", "description": "bounded output"}],
            "non_goals": ["no expansion"],
            "path_scope": {"read": ["src"], "write": ["src"]},
            "command_classes": {"test": {"allowed": True}},
            "evidence": [{"id": "tests", "class": "contract-test", "required": True}],
            "stop_conditions": [{"id": "stop", "kind": "deny"}],
        }
        with self.assertRaisesRegex(RuntimeError, "bound task spec"):
            CORE.bind_envelope_candidate(args, {})
        with (
            patch.object(CORE.execution_envelope, "load", return_value=(None, "", ["bad file"])),
            self.assertRaisesRegex(RuntimeError, "envelope is invalid: bad file"),
        ):
            CORE.bind_envelope_candidate(args, meta)
        with (
            patch.object(CORE.execution_envelope, "load", return_value=(envelope, "", [])),
            patch.object(CORE.execution_envelope, "errors", return_value=["injected"]),
            self.assertRaisesRegex(RuntimeError, "envelope is invalid"),
        ):
            CORE.bind_envelope_candidate(args, meta)
        for field, value, message in (
            ("task", "AR-9999", "task or spec"),
            ("spec_ref", "other.json", "task or spec"),
            ("task_revision", 2, "task revision"),
            ("spec_revision", 2, "spec revision"),
            ("envelope_revision", 2, "envelope revision"),
        ):
            candidate = {**envelope, field: value}
            with (
                patch.object(CORE.execution_envelope, "load", return_value=(candidate, "", [])),
                patch.object(CORE.execution_envelope, "errors", return_value=[]),
                self.assertRaisesRegex(RuntimeError, message),
            ):
                CORE.bind_envelope_candidate(args, meta)

    def test_execution_preflight_presentation_and_run_gate(self) -> None:
        """Expose every admission result without launching a command."""
        allowed = argparse.Namespace(task="AR-0001", owner="worker-a", json=True)
        with (
            patch.object(CORE, "execution_preflight", return_value=(True, "ALLOWED", "ok", 30)),
            patch("builtins.print") as output,
        ):
            self.assertEqual(0, CORE.print_execution_preflight(allowed))
        self.assertIn('"timeout_seconds":30', str(output.call_args))
        denied = argparse.Namespace(task="AR-0001", owner="worker-a", json=False)
        with (
            patch.object(CORE, "execution_preflight", return_value=(False, "DENIED", "no", None)),
            patch("builtins.print") as output,
        ):
            self.assertEqual(2, CORE.print_execution_preflight(denied))
        self.assertEqual("DENIED: no", output.call_args.args[0])
        self.make_task()
        self.assertIsNone(CORE.run_envelope_timeout(argparse.Namespace(task="AR-0001")))
        path, meta, body = CORE.locate("AR-0001")
        meta["execution_envelope_ref"] = "envelopes/AR-0001-e1.json"
        CORE.write_task(path, meta, body)
        with self.assertRaisesRegex(RuntimeError, "requires --command-class"):
            CORE.run_envelope_timeout(argparse.Namespace(task="AR-0001"))
        attempted = argparse.Namespace(task="AR-0001", command_class="test")
        with (
            patch.object(CORE, "execution_preflight", return_value=(False, "DENIED", "no", None)),
            self.assertRaisesRegex(RuntimeError, "preflight DENIED"),
        ):
            CORE.run_envelope_timeout(attempted)
        self.assertEqual([], attempted.scope_path)

    def test_execution_preflight_uses_durable_envelope_and_scope(self) -> None:
        """Admission derives mount paths from task authority, never caller files."""
        projects = self.root / "projects"
        worktree = projects / "candidate"
        (worktree / "src").mkdir(parents=True)
        CORE.CONFIG.parent.mkdir(exist_ok=True)
        CORE.CONFIG.write_text(json.dumps({"projects_root": str(projects)}), encoding="utf-8")
        envelope = {
            "schema_version": 1,
            "envelope_id": "AR-0001-e1",
            "envelope_revision": 1,
            "task": "AR-0001",
            "task_revision": 1,
            "spec_ref": "spec.json",
            "spec_revision": 1,
            "outputs": [{"id": "output", "description": "bounded output"}],
            "non_goals": ["no expansion"],
            "path_scope": {"read": ["src"], "write": ["src"]},
            "command_classes": {"source-write": {"allowed": True, "timeout_seconds": 30}},
            "evidence": [{"id": "tests", "class": "contract-test", "required": True}],
            "stop_conditions": [{"id": "stop", "kind": "deny"}],
        }
        self.make_task(
            status="in_progress",
            owner="worker-a",
            claim_expires="2099-01-01T00:00:00+00:00",
            worktree_key="candidate",
            spec_ref="spec.json",
            spec_revision=1,
            execution_envelope_ref="envelopes/AR-0001-e1.json",
            execution_envelope_revision=1,
            execution_envelope_task_revision=1,
            execution_envelope_digest=CORE.execution_envelope.canonical_digest(envelope),
            execution_envelope=envelope,
        )
        args = argparse.Namespace(
            task="AR-0001", owner="worker-a", command_class="source-write", scope_path=["src"]
        )
        allowed, code, _detail, timeout = CORE.execution_preflight(args)
        self.assertTrue(allowed)
        self.assertEqual("ALLOWED", code)
        self.assertEqual(30, timeout)
        self.assertEqual(str(worktree), args._envelope_worktree)
        self.assertEqual(["src"], args._envelope_write_scope)
        (worktree / "src").rmdir()
        denied = CORE.execution_preflight(args)
        self.assertEqual((False, "PATH_SCOPE_UNAVAILABLE"), denied[:2])
        (worktree / "src").mkdir()
        path, meta, body = CORE.locate("AR-0001")
        meta["worktree_key"] = ""
        CORE.write_task(path, meta, body)
        self.assertEqual((False, "WORKTREE_BINDING_REQUIRED"), CORE.execution_preflight(args)[:2])
        meta["worktree_key"] = "candidate"
        meta["execution_envelope_digest"] = "sha256:" + "0" * 64
        CORE.write_task(path, meta, body)
        self.assertEqual((False, "ENVELOPE_DIGEST_MISMATCH"), CORE.execution_preflight(args)[:2])
        meta["execution_envelope_digest"] = CORE.execution_envelope.canonical_digest(envelope)
        meta["execution_envelope"] = "invalid"
        CORE.write_task(path, meta, body)
        self.assertEqual((False, "ENVELOPE_MISSING"), CORE.execution_preflight(args)[:2])
        with patch.object(
            CORE.execution_envelope,
            "preflight",
            return_value=CORE.execution_envelope.Preflight(True, "ALLOWED", "fault injected"),
        ):
            self.assertEqual((False, "ENVELOPE_INVALID"), CORE.execution_preflight(args)[:2])
        meta["execution_envelope"] = envelope
        meta["execution_envelope_digest"] = "sha256:" + "0" * 64
        CORE.write_task(path, meta, body)
        with patch.object(
            CORE.execution_envelope,
            "preflight",
            return_value=CORE.execution_envelope.Preflight(True, "ALLOWED", "fault injected"),
        ):
            self.assertEqual((False, "ENVELOPE_INVALID"), CORE.execution_preflight(args)[:2])

    def test_command_journal_rejects_malformed_records(self) -> None:
        CORE.RUNTIME.mkdir(exist_ok=True)
        path = CORE.RUNTIME / "command-results.jsonl"
        path.write_text('{"argv_sha256":"bad"}\n', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "line 1"):
            CORE.legacy_command_results()
        path.write_text(
            json.dumps(
                {
                    "at": "now",
                    "task": "AR-0001",
                    "owner": "w",
                    "argv_sha256": "a" * 64,
                    "returncode": "bad",
                    "classification": "EXIT",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeError, "return code"):
            CORE.legacy_command_results()
        path.write_text(
            json.dumps(
                {
                    "at": "now",
                    "task": "AR-0001",
                    "owner": "w",
                    "argv_sha256": "not-a-digest",
                    "returncode": 0,
                    "classification": "EXIT",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeError, "journal digest"):
            CORE.legacy_command_results()

    def test_bound_run_rejects_timeout_above_envelope_cap_before_execution(self) -> None:
        """A bound timeout cap rejects before sandboxing, reserving, or subprocess launch."""
        args = argparse.Namespace(
            task="AR-0001",
            owner="worker-a",
            command=["would-not-run"],
            command_class="test",
            scope_path=[],
            timeout_seconds=31,
        )
        with (
            patch.object(CORE, "backend_selection", return_value={"backend": "sqlite"}),
            patch.object(CORE, "require_active_owner", return_value=None),
            patch.object(CORE, "run_envelope_timeout", return_value=30),
            patch.object(CORE, "sandboxed_envelope_command") as sandbox,
            self.assertRaisesRegex(RuntimeError, "exceeds envelope limit"),
        ):
            CORE._cmd_run_fenced(args)
        sandbox.assert_not_called()

    def test_invocation_worktree_ignores_non_product_and_state_roots(self) -> None:
        """Only a distinct configured product checkout becomes a worktree identity."""
        projects = self.root / "projects"
        projects.mkdir()
        CORE.CONFIG.parent.mkdir(exist_ok=True)
        CORE.CONFIG.write_text(
            json.dumps({"projects_root": str(projects), "product_worktree": "product"}),
            encoding="utf-8",
        )
        outside = self.root / "outside"
        outside.mkdir()
        with patch.object(CORE.Path, "cwd", return_value=outside):
            self.assertIsNone(CORE.invocation_worktree())
        CORE.CONFIG.write_text(
            json.dumps({"projects_root": str(self.root), "product_worktree": "product"}),
            encoding="utf-8",
        )
        with (
            patch.object(CORE.Path, "cwd", return_value=CORE.ROOT),
            patch.object(
                CORE,
                "run",
                return_value=subprocess.CompletedProcess([], 0, str(CORE.ROOT) + "\n", ""),
            ),
        ):
            self.assertIsNone(CORE.invocation_worktree())


def load_tests(
    _loader: unittest.TestLoader, _tests: unittest.TestSuite, _pattern: str | None
) -> unittest.TestSuite:
    """Discover only methods defined here, not every inherited fixture test."""
    suite = unittest.TestSuite()
    for name in ExecutionEnvelopeLifecycleTest.__dict__:
        if name.startswith("test_"):
            suite.addTest(ExecutionEnvelopeLifecycleTest(name))
    return suite
