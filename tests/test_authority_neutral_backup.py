# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile tests for the authority-neutral backup-only executor."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from tools.authority_neutral_backup import BackupExecutionError, execute_verified_backup

OPERATION = {"opcode": "backend.backup"}


def _context(backend: str = "git") -> dict[str, object]:
    root = Path(tempfile.gettempdir()).resolve() / "coordinator-artifacts"
    return {
        "backend": backend,
        "target": "new",
        "destination": str(root / "backup"),
        "artifact_root": str(root),
        "binding": {"project_id": "project"},
    }


class _GitAdapter:
    def execute_generated_backup(
        self, _operation: dict[str, object], _context: dict[str, object]
    ) -> dict[str, object]:
        return {
            "outcome": "completed",
            "backup_verified": True,
            "restore_roundtrip_verified": True,
            "backend_identity_verified": True,
            "mutates_authority": False,
        }


class _SQLiteAdapter:
    def __init__(self) -> None:
        self.destination: Path | None = None
        self.binding: dict[str, Any] | None = None

    def execute_generated_operation(
        self, _operation: dict[str, object], destination: Path, binding: dict[str, Any]
    ) -> dict[str, object]:
        self.destination = destination
        self.binding = binding
        return {
            "outcome": "completed",
            "backup_verified": True,
            "restore_roundtrip_verified": True,
            "backend_identity_verified": True,
            "mutates_authority": False,
        }


class AuthorityNeutralBackupTests(unittest.TestCase):
    def test_git_dispatch_returns_only_verified_non_mutating_evidence(self) -> None:
        result = execute_verified_backup(_GitAdapter(), OPERATION, _context())
        self.assertTrue(result["backup_verified"])
        self.assertFalse(result["mutates_authority"])

    def test_sqlite_dispatch_uses_bound_destination_and_binding(self) -> None:
        adapter = _SQLiteAdapter()
        context = _context("sqlite")
        result = execute_verified_backup(adapter, OPERATION, context)
        self.assertEqual(Path(str(context["destination"])), adapter.destination)
        self.assertEqual(context["binding"], adapter.binding)
        self.assertEqual("completed", result["outcome"])

    def test_rejects_destination_escape_before_backend_call(self) -> None:
        context = _context()
        context["destination"] = str(Path(str(context["artifact_root"])).parent / "escape")
        with self.assertRaisesRegex(BackupExecutionError, "escapes artifact root"):
            execute_verified_backup(_GitAdapter(), OPERATION, context)

    def test_rejects_mutating_or_incomplete_backend_result(self) -> None:
        class Mutating:
            def execute_generated_backup(
                self, _operation: object, _context: object
            ) -> dict[str, object]:
                return {
                    "outcome": "completed",
                    "backup_verified": True,
                    "restore_roundtrip_verified": True,
                    "backend_identity_verified": True,
                    "mutates_authority": True,
                }

        with self.assertRaisesRegex(BackupExecutionError, "postconditions"):
            execute_verified_backup(Mutating(), OPERATION, _context())

    def test_rejects_wrong_opcode_and_backend(self) -> None:
        with self.assertRaisesRegex(BackupExecutionError, "unsupported"):
            execute_verified_backup(_GitAdapter(), {"opcode": "selector.commit"}, _context())
        invalid = _context("other")
        with self.assertRaisesRegex(BackupExecutionError, "identity"):
            execute_verified_backup(_GitAdapter(), OPERATION, invalid)

    def test_rejects_incomplete_and_malformed_context(self) -> None:
        with self.assertRaisesRegex(BackupExecutionError, "context is incomplete"):
            execute_verified_backup(_GitAdapter(), OPERATION, {})
        context = _context()
        context.pop("destination")
        with self.assertRaisesRegex(BackupExecutionError, "context is incomplete"):
            execute_verified_backup(_GitAdapter(), OPERATION, context)
        context = _context()
        context["destination"] = object()
        with self.assertRaisesRegex(BackupExecutionError, "artifact binding"):
            execute_verified_backup(_GitAdapter(), OPERATION, context)
        context = _context()
        context["binding"] = []
        with self.assertRaisesRegex(BackupExecutionError, "authority binding"):
            execute_verified_backup(_GitAdapter(), OPERATION, context)

    def test_rejects_incomplete_or_malformed_backend_results(self) -> None:
        class Incomplete:
            def execute_generated_backup(
                self, _operation: object, _context: object
            ) -> dict[str, object]:
                return {"outcome": "completed"}

        class NonMapping:
            def execute_generated_backup(self, _operation: object, _context: object) -> str:
                return "invalid"

        with self.assertRaisesRegex(BackupExecutionError, "result is incomplete"):
            execute_verified_backup(Incomplete(), OPERATION, _context())
        with self.assertRaisesRegex(BackupExecutionError, "result is invalid"):
            execute_verified_backup(NonMapping(), OPERATION, _context())

    def test_wraps_backend_failures_as_backup_errors(self) -> None:
        class Failing:
            def execute_generated_backup(
                self, _operation: object, _context: object
            ) -> dict[str, object]:
                raise RuntimeError("injected failure")

        with self.assertRaisesRegex(BackupExecutionError, "execution failed"):
            execute_verified_backup(Failing(), OPERATION, _context())

        class AlreadyClassified:
            def execute_generated_backup(
                self, _operation: object, _context: object
            ) -> dict[str, object]:
                raise BackupExecutionError("already classified")

        with self.assertRaisesRegex(BackupExecutionError, "already classified"):
            execute_verified_backup(AlreadyClassified(), OPERATION, _context())


if __name__ == "__main__":
    unittest.main()
