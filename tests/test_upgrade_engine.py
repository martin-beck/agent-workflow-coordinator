# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Failure-path tests for the durable upgrade phase journal."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

from tools.upgrade_admission import (
    PREFLIGHT_PREDICATES,
    QUIESCENCE_PREDICATES,
    REOPEN_PREDICATES,
)
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
ADMISSION = {
    **dict.fromkeys(PREFLIGHT_PREDICATES, True),
    **dict.fromkeys(QUIESCENCE_PREDICATES, True),
    **dict.fromkeys(REOPEN_PREDICATES, True),
    "operation_id": "op-1",
    "state_revision": 1,
    "fencing_token": "fence-1",
    "fencing_owner": "worker-1",
    "durable_barrier_id": "barrier-1",
    "target": "new",
    "validation_failed": False,
}


class FakeAdapter:
    def snapshot(self, _phase: str, context: object) -> dict[str, object]:
        identity = dict(context) if isinstance(context, Mapping) else CONTEXT
        return {
            **ADMISSION,
            **{
                field: identity[field]
                for field in ("operation_id", "state_revision", "fencing_token", "fencing_owner")
            },
        }

    def execute(self, phase: str, _context: object) -> dict[str, object]:
        result: dict[str, object] = {"backend": "sqlite", "fencing_token": "fence-1"}
        if phase == "rollback":
            result.update(
                restored_verified=True,
                runtime_validated=True,
                backend_roundtrip_valid=True,
            )
        return result


class UpgradeEngineTests(unittest.TestCase):
    def test_apply_is_ordered_and_idempotent(self) -> None:  # noqa: C901
        with tempfile.TemporaryDirectory() as directory:
            engine = UpgradeEngine(
                "op-1", Path(directory) / "journal.json", CONTEXT, backend_adapter=FakeAdapter()
            )
            engine.plan()
            seen: list[str] = []

            def handler(  # noqa: C901
                _operation: str, _state: object, phase: str = ""
            ) -> dict[str, object]:
                seen.append(phase)
                result: dict[str, object] = {
                    "phase": phase,
                    "backend": "sqlite",
                    "fencing_token": "fence-1",
                    "backend_identity_verified": True,
                }
                if phase == "discover":
                    result.update(release_authentic=True, runtime_supported=True)
                if phase == "preflight":
                    result.update(preflight_admitted=True, capacity_verified=True)
                if phase == "quiesce":
                    result.update(
                        barrier_acquired=True,
                        workers_drained=True,
                        leases_fenced=True,
                        fencing_verified=True,
                    )
                if phase == "backup":
                    result.update(backup_verified=True, restore_roundtrip_verified=True)
                if phase == "stage":
                    result.update(staged_verified=True, manifest_verified=True)
                if phase == "commit":
                    result.update(
                        quiesced=True,
                        backup_verified=True,
                        selector_verified=True,
                        selector_commit_atomic=True,
                        fencing_verified=True,
                        selector_before_verified=True,
                        selector_after_verified=True,
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
                if phase == "preflight":
                    result["preflight_snapshot"] = ADMISSION
                if phase == "quiesce":
                    result["quiescence_snapshot"] = ADMISSION
                if phase == "commit":
                    result.update(admitted_snapshot=ADMISSION, current_snapshot=ADMISSION)
                if phase == "reopen":
                    result["reopen_snapshot"] = ADMISSION
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
                "op-2",
                Path(directory) / "journal.json",
                {**CONTEXT, "operation_id": "op-2"},
                backend_adapter=FakeAdapter(),
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

    def test_commit_validate_and_reopen_require_safety_evidence(self) -> None:  # noqa: C901
        with tempfile.TemporaryDirectory() as directory:
            engine = UpgradeEngine(
                "op-evidence",
                Path(directory) / "journal.json",
                {**CONTEXT, "operation_id": "op-evidence"},
                backend_adapter=FakeAdapter(),
            )
            engine.plan()

            def evidence_handler(phase: str) -> Handler:  # noqa: C901
                def run(  # noqa: C901
                    _operation: str, _state: object
                ) -> dict[str, object]:
                    result: dict[str, object] = {
                        "backend": "sqlite",
                        "fencing_token": "fence-1",
                        "backend_identity_verified": True,
                        "mutates_authority": phase == "commit",
                    }
                    if phase == "discover":
                        result.update(release_authentic=True, runtime_supported=True)
                    if phase == "preflight":
                        result.update(preflight_admitted=True, capacity_verified=True)
                    if phase == "quiesce":
                        result.update(
                            barrier_acquired=True,
                            workers_drained=True,
                            leases_fenced=True,
                            fencing_verified=True,
                        )
                    if phase == "backup":
                        result.update(backup_verified=True, restore_roundtrip_verified=True)
                    if phase == "stage":
                        result.update(staged_verified=True, manifest_verified=True)
                    if phase == "preflight":
                        result["preflight_snapshot"] = ADMISSION
                    if phase == "quiesce":
                        result["quiescence_snapshot"] = ADMISSION
                    if phase == "commit":
                        result.update(admitted_snapshot=ADMISSION, current_snapshot=ADMISSION)
                    if phase == "reopen":
                        result["reopen_snapshot"] = ADMISSION
                    return result

                return run

            handlers: dict[str, Handler] = {phase: evidence_handler(phase) for phase in PHASES}
            with self.assertRaises(UpgradeError):
                engine.apply(handlers)
            journal = engine._load()
            self.assertEqual(journal["phase"], "commit")
            self.assertEqual(journal["status"], "failed")

    def test_missing_plan_and_duplicate_plan_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.json"
            engine = UpgradeEngine(
                "op-3", journal, {**CONTEXT, "operation_id": "op-3"}, backend_adapter=FakeAdapter()
            )
            with self.assertRaises(UpgradeError):
                engine.apply({})
            engine.plan()
            with self.assertRaises(UpgradeError):
                engine.plan()

    def test_context_and_journal_tampering_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.json"
            engine = UpgradeEngine(
                "op-4", journal, {**CONTEXT, "operation_id": "op-4"}, backend_adapter=FakeAdapter()
            )
            engine.plan()
            value = json.loads(journal.read_text())
            value["context"]["backend"] = "git"
            journal.write_text(json.dumps(value))
            with self.assertRaises(UpgradeError):
                engine.apply({})

    def test_started_phase_requires_explicit_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.json"
            engine = UpgradeEngine(
                "op-5", journal, {**CONTEXT, "operation_id": "op-5"}, backend_adapter=FakeAdapter()
            )
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
