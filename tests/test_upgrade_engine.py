# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Failure-path tests for the durable upgrade phase journal."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.upgrade_engine import PHASES, Handler, UpgradeEngine, UpgradeError


class UpgradeEngineTests(unittest.TestCase):
    def test_apply_is_ordered_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = UpgradeEngine("op-1", Path(directory) / "journal.json")
            engine.plan()
            seen: list[str] = []

            def handler(_operation: str, _state: object, phase: str = "") -> dict[str, str]:
                seen.append(phase)
                return {"phase": phase}

            def make_handler(phase: str) -> Handler:
                def run(operation: str, state: object) -> dict[str, str]:
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
            engine = UpgradeEngine("op-2", Path(directory) / "journal.json")
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
            engine = UpgradeEngine("op-evidence", Path(directory) / "journal.json")
            engine.plan()
            handlers: dict[str, Handler] = {
                phase: (lambda _operation, _state: {}) for phase in PHASES
            }
            with self.assertRaises(UpgradeError):
                engine.apply(handlers)
            journal = engine._load()
            self.assertEqual(journal["phase"], "commit")
            self.assertEqual(journal["status"], "failed")

    def test_missing_plan_and_duplicate_plan_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.json"
            engine = UpgradeEngine("op-3", journal)
            with self.assertRaises(UpgradeError):
                engine.apply({})
            engine.plan()
            with self.assertRaises(UpgradeError):
                engine.plan()


if __name__ == "__main__":
    unittest.main()
