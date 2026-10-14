# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Command-boundary coverage for execution-envelope lifecycle operations."""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_handoffctl import CORE

roles = importlib.import_module("roles")
upgrade_commands = importlib.import_module("upgrade_commands")


class ExecutionEnvelopeCommandBoundaryTest(unittest.TestCase):
    """Exercise positive and fail-closed routes around bound execution commands."""

    def test_fenced_run_rejects_nonpositive_timeout_before_sandbox_or_reservation(self) -> None:
        """An invalid timeout cannot reach Bubblewrap or create a durable reservation."""
        args = argparse.Namespace(
            task="AR-0001",
            owner="worker-a",
            command=["would-not-run"],
            command_class="test",
            scope_path=[],
            timeout_seconds=0,
        )
        with (
            patch.object(CORE, "backend_selection", return_value={"backend": "sqlite"}),
            patch.object(CORE, "require_active_owner", return_value=CORE.DEFAULT_EVIDENCE_POLICY),
            patch.object(CORE, "run_envelope_timeout", return_value=None),
            patch.object(CORE, "sandboxed_envelope_command") as sandbox,
            patch.object(CORE, "mutate") as reserve,
            self.assertRaisesRegex(RuntimeError, "timeout must be positive"),
        ):
            CORE._cmd_run_fenced(args)
        sandbox.assert_not_called()
        reserve.assert_not_called()

    def test_execution_lock_rejects_invalid_task_identity(self) -> None:
        """A malformed task name cannot select or create another task's fence."""
        with self.assertRaisesRegex(RuntimeError, "invalid task execution fence"):
            CORE.task_execution_lock_path("../AR-0001")

    def test_lifecycle_and_run_dispatch_routes_execution_commands(self) -> None:
        """Bound dispatch keeps reservation mutations and command argv handling distinct."""
        lifecycle = argparse.Namespace(cmd="recover-execution")
        run = argparse.Namespace(cmd="run", command=["--", "echo", "ok"])
        with (
            patch.object(CORE, "mutate", return_value="reservation released") as mutate,
            patch.object(CORE, "cmd_run", return_value=23) as cmd_run,
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(0, CORE.dispatch_bound_standard_command(lifecycle))
            self.assertEqual(23, CORE.dispatch_bound_standard_command(run))
        mutate.assert_called_once_with(lifecycle, "recover-execution")

        lifecycle = argparse.Namespace(cmd="abandon-execution")
        with patch.object(CORE, "mutate") as mutate:
            CORE.dispatch_bound_command(lifecycle)
        mutate.assert_called_once_with(lifecycle, "abandon-execution")
        cmd_run.assert_called_once_with(run)
        self.assertEqual(["echo", "ok"], run.command)
        self.assertIn("reservation released", output.getvalue())

    def test_read_only_dispatch_selects_each_projection(self) -> None:
        """Read-only execution preflight remains separate from state-changing routes."""
        commands = (
            (argparse.Namespace(cmd="snapshot", task="AR-0001"), "cmd_snapshot"),
            (argparse.Namespace(cmd="board"), "cmd_board"),
            (argparse.Namespace(cmd="metrics"), "cmd_metrics"),
            (argparse.Namespace(cmd="preflight", task="AR-0001"), "print_execution_preflight"),
        )
        for args, name in commands:
            with patch.object(CORE, name, return_value=41) as selected:
                self.assertEqual(
                    41 if name == "print_execution_preflight" else 0,
                    CORE.dispatch_read_only_command(args),
                )
            selected.assert_called_once_with(
                args.task
            ) if name == "cmd_snapshot" else selected.assert_called_once_with(
                args
            ) if name == "print_execution_preflight" else selected.assert_called_once_with()


class HandoffctlCommandDispatchTest(unittest.TestCase):
    """Cover public command adapters whose failures must not be silently rerouted."""

    def test_roles_dispatches_each_operation_and_translates_domain_errors(self) -> None:
        """Each roles operation preserves its exact argument set and a clear error boundary."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = {
                "state": root / "roles.json",
                "registry": root / "registry.json",
                "expected_revision": 4,
                "assignment_id": "assignment-1",
                "owner_id": "worker-a",
                "role_id": "implementer",
                "expires_at": "2099-01-01T00:00:00+00:00",
                "evidence_kind": "test",
                "evidence_ref": "receipt",
                "evidence_digest": "sha256:" + "a" * 64,
            }
            cases = (
                ("assign", "assign"),
                ("list", "list_assignments"),
                ("check", "check"),
                ("remove", "remove"),
            )
            for command, function in cases:
                args = argparse.Namespace(roles_command=command, **base)
                with patch.object(
                    roles, function, return_value={"operation": command}
                ) as operation:
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(0, CORE.dispatch_roles_command(args))
                self.assertIn(f'"operation":"{command}"', output.getvalue())
                self.assertTrue(operation.called)

            args = argparse.Namespace(roles_command="check", **base)
            with (
                patch.object(roles, "check", side_effect=roles.RolesError("invalid role evidence")),
                self.assertRaisesRegex(RuntimeError, "invalid role evidence"),
            ):
                CORE.dispatch_roles_command(args)

    def test_upgrade_consume_passes_complete_runtime_identity(self) -> None:
        """The non-mutating upgrade consumer retains all signed runtime identity fields."""
        args = argparse.Namespace(
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
            upgrade_commands, "consume_selected_runtime_command", return_value=17
        ) as consume:
            self.assertEqual(17, CORE.cmd_upgrade(args))
        identity = consume.call_args.args[2]
        self.assertEqual("a" * 40, identity.source_commit)
        self.assertEqual(("python", "-m", "worker"), consume.call_args.args[4])

    def test_upgrade_mutation_requires_binding_before_execution(self) -> None:
        """Apply and rollback reject absent live binding without invoking the engine."""
        args = argparse.Namespace(upgrade_action="apply", binding=None, contract="contract.json")
        with (
            patch.object(CORE, "backend_selection", return_value={"backend": "sqlite"}),
            patch("tools.upgrade_commands.execute_upgrade_command") as execute,
            self.assertRaisesRegex(RuntimeError, "require --binding"),
        ):
            CORE.cmd_upgrade(args)
        execute.assert_not_called()

    def test_bound_standard_dispatches_maintenance_commands(self) -> None:
        """Maintenance commands retain their explicit public dispatcher routes."""
        cases = (
            (argparse.Namespace(cmd="reconcile", commit=True, push=False), "reconcile", 0),
            (argparse.Namespace(cmd="checkpoint"), "cmd_checkpoint", 0),
            (argparse.Namespace(cmd="rollback"), "cmd_rollback", 0),
            (argparse.Namespace(cmd="doctor", live=True), "cmd_doctor", 31),
            (argparse.Namespace(cmd="render-status", check=True), "cmd_render_status", 0),
            (argparse.Namespace(cmd="migrate"), "cmd_migrate", 0),
            (argparse.Namespace(cmd="upgrade"), "cmd_upgrade", 37),
        )
        for args, function, expected in cases:
            with patch.object(CORE, function, return_value=expected) as operation:
                self.assertEqual(expected, CORE.dispatch_bound_standard_command(args))
            if function == "reconcile":
                operation.assert_called_once_with(do_commit=True, push=False)
            elif function in {"cmd_doctor"}:
                operation.assert_called_once_with(live=True)
            elif function == "cmd_render_status":
                operation.assert_called_once_with(check=True)
            else:
                operation.assert_called_once_with(args)


if __name__ == "__main__":
    unittest.main()
