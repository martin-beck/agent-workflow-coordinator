# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Contract coverage for bounded retry-circuit admission and recovery."""

from __future__ import annotations

import argparse
import contextlib
import unittest

from tools import retry_circuit, run_command


class FailingRunRuntime:
    ROOT = object()
    retry_circuit = retry_circuit

    def __init__(self) -> None:
        self.meta: retry_circuit.Meta = {
            "id": "AR-0001",
            "owner": "worker",
            "status": "in_progress",
            "task_revision": 2,
        }
        self.calls: list[str] = []

    def task_execution_locked(
        self, *_args: object, **_kwargs: object
    ) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    def _cmd_run_fenced(
        self, _args: argparse.Namespace
    ) -> tuple[object, str, float, bool, int, None, None]:
        return object(), "a" * 64, 1.0, False, 1, None, None

    def append_command_result(self, *_args: object) -> None:
        self.calls.append("journal")

    def locate(self, _task: str) -> tuple[None, retry_circuit.Meta, str]:
        return None, self.meta, ""

    def mutate(self, args: argparse.Namespace, kind: str, _policy: object) -> None:
        self.calls.append(kind)
        if kind == "retry-circuit-record":
            retry_circuit.record_failure(
                self.meta,
                args.retry_circuit_identity,
                args.retry_circuit_failure,
                args.retry_window,
                "now",
            )
        self.meta["task_revision"] += 1

    def backend_selection(self) -> dict[str, str]:
        return {"backend": "sqlite"}

    def now(self) -> str:
        return "now"

    def require_policy_unchanged(self, *_args: object) -> None:
        self.calls.append("policy")

    def reconcile(self, **_kwargs: object) -> None:
        self.calls.append("reconcile")

    class PostCommandReconcileError(RuntimeError):
        pass


class RetryCircuitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.meta: retry_circuit.Meta = {"id": "AR-0001", "task_revision": 2}
        self.identity = retry_circuit.normalized_identity("a" * 64, "test", ["tests"])

    def _failure(self, revision: int, at: str) -> str:
        self.meta["task_revision"] = revision
        return retry_circuit.record_failure(self.meta, self.identity, "EXIT", 3, at)

    def test_normalized_identity_is_stable_and_never_retains_argv(self) -> None:
        first = retry_circuit.normalized_identity("a" * 64, "test", ["src", "tests"])
        second = retry_circuit.normalized_identity("a" * 64, "test", ["tests", "src"])
        self.assertEqual(first, second)
        with self.assertRaisesRegex(ValueError, "scope"):
            retry_circuit.normalized_identity("a" * 64, "test", ["../escape"])

    def test_bounded_unchanged_failures_open_and_hold_the_exact_identity(self) -> None:
        self.assertIn("1/3", self._failure(2, "one"))
        self.assertIn("2/3", self._failure(3, "two"))
        self.assertIn("opened", self._failure(4, "three"))
        with self.assertRaisesRegex(RuntimeError, "RETRY_CIRCUIT_OPEN"):
            retry_circuit.admit(self.meta, self.identity)
        other = retry_circuit.normalized_identity("b" * 64, "test", ["tests"])
        retry_circuit.admit(self.meta, other)

    def test_changed_durable_progress_reopens_without_erasing_auditable_hold(self) -> None:
        for revision in range(2, 5):
            self._failure(revision, str(revision))
        self.meta["checkpoint_commit"] = "c" * 40
        retry_circuit.admit(self.meta, self.identity)
        self.assertIsNotNone(self.meta["retry_circuit"]["hold"])

    def test_recovery_routes_are_explicit_and_checkpoint_requires_checkpoint(self) -> None:
        for revision in range(2, 5):
            self._failure(revision, str(revision))
        self.meta["task_revision"] = 5
        with self.assertRaisesRegex(RuntimeError, "recorded checkpoint"):
            retry_circuit.recover(self.meta, self.identity, "checkpoint", "saved", "four")
        self.assertIn(
            "changed-approach",
            retry_circuit.recover(
                self.meta, self.identity, "changed-approach", "use a new fixture", "five"
            ),
        )
        retry_circuit.admit(self.meta, self.identity)
        self.assertEqual("changed-approach", self.meta["retry_circuit"]["recoveries"][-1]["kind"])

    def test_malicious_tampering_and_window_changes_fail_closed(self) -> None:
        self._failure(2, "one")
        self.meta["task_revision"] = 3
        with self.assertRaisesRegex(RuntimeError, "window changed"):
            retry_circuit.record_failure(self.meta, self.identity, "EXIT", 4, "two")
        corrupted = dict(self.meta["retry_circuit"])
        corrupted["attempts"] = []
        with self.assertRaisesRegex(ValueError, "attempts"):
            retry_circuit.validate(corrupted)

    def test_recovery_apply_fences_owner_revision_and_attempt_identity(self) -> None:
        meta: retry_circuit.Meta = {
            "id": "AR-0001",
            "owner": "worker",
            "status": "in_progress",
            "task_revision": 2,
        }
        for revision in range(2, 5):
            meta["task_revision"] = revision
            retry_circuit.record_failure(meta, self.identity, "EXIT", 3, str(revision))
        meta["task_revision"] = 5
        args = argparse.Namespace(
            owner="worker",
            expected_revision=5,
            attempt=self.identity,
            retry_circuit_action="retry-exception",
            reason="known transient test provider failure",
        )
        self.assertIn("recovered", retry_circuit.apply(args, meta, "now"))
        args.owner = "other"
        with self.assertRaisesRegex(RuntimeError, "active task owner"):
            retry_circuit.apply(args, meta, "later")

    def test_parser_requires_fenced_recovery_arguments(self) -> None:
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        retry_circuit.add_command_parser(commands)
        values = parser.parse_args(
            [
                "retry-circuit",
                "changed-approach",
                "AR-0001",
                "--owner",
                "worker",
                "--expected-revision",
                "2",
                "--attempt",
                self.identity,
                "--reason",
                "new method",
            ]
        )
        self.assertEqual("changed-approach", values.retry_circuit_action)
        with self.assertRaises(SystemExit):
            parser.parse_args(["retry-circuit", "retry-exception", "AR-0001"])

    def test_nonzero_wrapped_command_records_before_ordinary_completion_update(self) -> None:
        runtime = FailingRunRuntime()
        result = run_command.cmd_run(
            runtime,
            argparse.Namespace(
                task="AR-0001",
                owner="worker",
                command=["false"],
                command_class=None,
                scope_path=[],
                retry_window=3,
            ),
        )
        self.assertEqual(1, result)
        self.assertEqual(
            ["journal", "retry-circuit-record", "update", "policy", "reconcile"], runtime.calls
        )
        self.assertEqual(1, len(runtime.meta["retry_circuit"]["attempts"]))
