# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Failure-path tests for the durable upgrade phase journal."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.upgrade_engine import PHASES, Handler, UpgradeEngine, UpgradeError

CONTEXT = {
    "operation_id": "op-1",
    "state_revision": 1,
    "fencing_token": "fence-1",
    "fencing_owner": "worker-1",
    "backend": "sqlite",
    "authority_revision": "authority-1",
    "target": "new",
}


class UpgradeEngineTests(unittest.TestCase):
    def test_apply_is_ordered_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = UpgradeEngine("op-1", Path(directory) / "journal.json", CONTEXT)
            engine.plan()
            seen: list[str] = []

            def handler(_operation: str, _state: object, phase: str = "") -> dict[str, object]:
                seen.append(phase)
                result: dict[str, object] = {"phase": phase}
                if phase == "commit":
                    result.update(
                        quiesced=True,
                        backup_verified=True,
                        selector_verified=True,
                        selector_commit_atomic=True,
                        fencing_verified=True,
                    )
                if phase == "validate":
                    result.update(
                        runtime_validated=True,
                        backend_roundtrip_valid=True,
                        projections_valid=True,
                        binding_valid=True,
                    )
                if phase == "reopen":
                    result.update(validated=True, barrier_held=True)
                result["mutates_authority"] = phase == "commit"
                return result

            def make_handler(phase: str) -> Handler:
                def run(operation: str, state: object) -> dict[str, object]:
                    return handler(operation, state, phase)

                return run

            handlers: dict[str, Handler] = {phase: make_handler(phase) for phase in PHASES}
            result = engine.apply(handlers)
            self.assertEqual(seen, list(PHASES))
            self.assertEqual(result["status"], "completed")
            engine.apply(handlers)
            self.assertEqual(seen, list(PHASES))

    def test_failure_is_durable_and_rollback_can_enter_safe_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = UpgradeEngine(
                "op-2", Path(directory) / "journal.json", {**CONTEXT, "operation_id": "op-2"}
            )
            engine.plan()

            def fail(_operation: str, _state: object) -> None:
                raise OSError("ambiguous")

            handlers: dict[str, Handler] = {
                phase: (fail if phase == "commit" else lambda _operation, _state: {})
                for phase in PHASES
            }
            with self.assertRaises(UpgradeError):
                engine.apply(handlers)
            with self.assertRaises(UpgradeError):
                engine.rollback(fail)
            self.assertEqual(engine._load()["status"], "safe-mode")

    def test_commit_validate_and_reopen_require_safety_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = UpgradeEngine(
                "op-evidence",
                Path(directory) / "journal.json",
                {**CONTEXT, "operation_id": "op-evidence"},
            )
            engine.plan()

            def evidence_handler(phase: str) -> Handler:
                return lambda _operation, _state: {"mutates_authority": phase == "commit"}

            handlers: dict[str, Handler] = {phase: evidence_handler(phase) for phase in PHASES}
            with self.assertRaises(UpgradeError):
                engine.apply(handlers)
            journal = engine._load()
            self.assertEqual(journal["phase"], "commit")
            self.assertEqual(journal["status"], "failed")

    def test_missing_plan_and_duplicate_plan_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.json"
            engine = UpgradeEngine("op-3", journal, {**CONTEXT, "operation_id": "op-3"})
            with self.assertRaises(UpgradeError):
                engine.apply({})
            engine.plan()
            with self.assertRaises(UpgradeError):
                engine.plan()

    def test_context_and_journal_tampering_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.json"
            engine = UpgradeEngine("op-4", journal, {**CONTEXT, "operation_id": "op-4"})
            engine.plan()
            value = json.loads(journal.read_text())
            value["context"]["backend"] = "git"
            journal.write_text(json.dumps(value))
            with self.assertRaises(UpgradeError):
                engine.apply({})

    def test_started_phase_requires_explicit_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.json"
            engine = UpgradeEngine("op-5", journal, {**CONTEXT, "operation_id": "op-5"})
            engine.plan()
            value = json.loads(journal.read_text())
            value["status"] = "running"
            value["phase"] = "discover"
            value["records"] = [
                {"operation_id": "op-5:discover", "phase": "discover", "outcome": "started"}
            ]
            journal.write_text(json.dumps(value))
            with self.assertRaises(UpgradeError):
                engine.apply({})


if __name__ == "__main__":
    unittest.main()
