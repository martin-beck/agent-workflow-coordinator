# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Failure-path tests for the durable upgrade phase journal."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from tools.upgrade_admission import (
    PREFLIGHT_PREDICATES,
    QUIESCENCE_PREDICATES,
    REOPEN_PREDICATES,
)
from tools.upgrade_engine import PHASES, Handler, UpgradeEngine, UpgradeError

CONTEXT = {
    "operation_id": "op-1",
    "project_id": "11111111-1111-4111-8111-111111111111",
    "state_revision": 1,
    "fencing_token": "fence-1",
    "fencing_owner": "worker-1",
    "backend": "sqlite",
    "authority_revision": "authority-1",
    "durable_barrier_id": "barrier-1",
    "barrier_identity_digest": "a" * 64,
    "envelope_digest": "b" * 64,
    "target": "new",
}
ROLLBACK_CONTEXT = {
    **CONTEXT,
    "target": "rollback",
    "barrier_identity_digest": "c" * 64,
    "envelope_digest": "d" * 64,
}
ADMISSION = {
    **dict.fromkeys(PREFLIGHT_PREDICATES, True),
    **dict.fromkeys(QUIESCENCE_PREDICATES, True),
    **dict.fromkeys(REOPEN_PREDICATES, True),
    "operation_id": "op-1",
    "project_id": "11111111-1111-4111-8111-111111111111",
    "state_revision": 1,
    "fencing_token": "fence-1",
    "fencing_owner": "worker-1",
    "backend": "sqlite",
    "authority_revision": "authority-1",
    "durable_barrier_id": "barrier-1",
    "barrier_identity_digest": "a" * 64,
    "envelope_digest": "b" * 64,
    "target": "new",
    "validation_failed": False,
}


class FakeAdapter:
    def snapshot(self, phase: str, context: object) -> dict[str, object]:
        identity = dict(context) if isinstance(context, Mapping) else CONTEXT
        if phase == "rollback":
            return {
                **ROLLBACK_CONTEXT,
                "operation_id": identity["operation_id"],
                "rollback_context_verified": True,
            }
        return {
            **ADMISSION,
            **{
                field: identity[field]
                for field in (
                    "operation_id",
                    "project_id",
                    "state_revision",
                    "fencing_token",
                    "fencing_owner",
                    "authority_revision",
                    "durable_barrier_id",
                    "barrier_identity_digest",
                    "envelope_digest",
                    "target",
                )
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


class FailingAdapter(FakeAdapter):
    def snapshot(self, _phase: str, _context: object) -> dict[str, object]:
        raise RuntimeError("authority probe failed")


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

    def test_context_digest_and_target_shapes_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for field, value in (
                ("backend", "unknown"),
                ("target", "upgrade"),
                ("barrier_identity_digest", "not-a-digest"),
                ("envelope_digest", "not-a-digest"),
            ):
                with self.subTest(field=field), self.assertRaises(UpgradeError):
                    UpgradeEngine(
                        "op-shape",
                        Path(directory) / f"{field}.json",
                        {**CONTEXT, field: value},
                        backend_adapter=FakeAdapter(),
                    )

    def test_context_and_journal_tampering_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.json"
            engine = UpgradeEngine(
                "op-4", journal, {**CONTEXT, "operation_id": "op-4"}, backend_adapter=FakeAdapter()
            )
            engine.plan()
            with self.assertRaises(UpgradeError):
                engine.rollback(lambda _step, _state: {})
            value = json.loads(journal.read_text())
            value["context"]["backend"] = "git"
            journal.write_text(json.dumps(value))
            with self.assertRaises(UpgradeError):
                engine.apply({})

    def test_every_journal_record_context_field_is_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.json"
            engine = UpgradeEngine(
                "op-record",
                journal,
                {**CONTEXT, "operation_id": "op-record"},
                backend_adapter=FakeAdapter(),
            )
            engine.plan()
            base = json.loads(journal.read_text())
            base["status"] = "running"
            base["phase"] = "discover"
            base["records"] = [
                {
                    "operation_id": "op-record",
                    "step_id": "op-record.discover",
                    "phase": "discover",
                    "outcome": "started",
                    "context": {**CONTEXT, "operation_id": "op-record"},
                }
            ]
            for field in CONTEXT:
                tampered = json.loads(json.dumps(base))
                tampered["records"][0]["context"][field] = "tampered"
                journal.write_text(json.dumps(tampered))
                with self.subTest(field=field), self.assertRaises(UpgradeError):
                    engine._load()

    def test_rollback_record_schema_and_target_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.json"
            engine = UpgradeEngine(
                "op-rollback",
                journal,
                {**CONTEXT, "operation_id": "op-rollback"},
                backend_adapter=FakeAdapter(),
            )
            engine.plan()
            base = json.loads(journal.read_text())
            record = {
                "operation_id": "op-rollback",
                "step_id": "op-rollback.rollback",
                "phase": "rollback",
                "outcome": "started",
                "context": {**CONTEXT, "operation_id": "op-rollback", "target": "rollback"},
            }
            for mutation in (
                {
                    "context": {
                        **cast(dict[str, object], record["context"]),
                        "target": "new",
                    }
                },
                {"extra": True},
                {"phase": None},
            ):
                value = json.loads(json.dumps(base))
                value["records"] = [{**record, **mutation}]
                journal.write_text(json.dumps(value))
                with self.subTest(mutation=mutation), self.assertRaises(UpgradeError):
                    engine._load()

    def test_successful_rollback_requires_and_writes_terminal_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.json"
            engine = UpgradeEngine(
                "op-success-rollback",
                journal,
                {**CONTEXT, "operation_id": "op-success-rollback"},
                backend_adapter=FakeAdapter(),
            )
            engine.plan()
            value = json.loads(journal.read_text())
            value["status"] = "failed"
            value["phase"] = "discover"
            value["records"] = [
                {
                    "operation_id": "op-success-rollback",
                    "step_id": "op-success-rollback.discover",
                    "phase": "discover",
                    "outcome": "failed",
                    "error": "failed",
                    "context": {**CONTEXT, "operation_id": "op-success-rollback"},
                }
            ]
            journal.write_text(json.dumps(value))
            result = engine.rollback(lambda _step, _state: {})
            self.assertEqual("rolled-back", result["status"])
            self.assertEqual("rollback_completed", result["records"][-1]["outcome"])

            reloaded = UpgradeEngine(
                "op-success-rollback",
                journal,
                {**CONTEXT, "operation_id": "op-success-rollback"},
                backend_adapter=FakeAdapter(),
            )
            self.assertEqual("rollback", reloaded._load()["phase"])

    def test_rollback_requires_adapter_verified_context_and_cannot_forge_evidence(self) -> None:
        class UnverifiedAdapter(FakeAdapter):
            def snapshot(self, phase: str, context: object) -> dict[str, object]:
                result = super().snapshot(phase, context)
                if phase == "rollback":
                    result["rollback_context_verified"] = False
                return result

        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.json"
            engine = UpgradeEngine(
                "op-unverified",
                journal,
                {**CONTEXT, "operation_id": "op-unverified"},
                backend_adapter=UnverifiedAdapter(),
            )
            engine.plan()
            with self.assertRaises(UpgradeError):
                engine.rollback(lambda _step, _state: {})
            self.assertEqual([], json.loads(journal.read_text())["records"])

    def test_rollback_rejects_unbound_adapter_context_before_journal(self) -> None:
        class MismatchedAdapter(FakeAdapter):
            def snapshot(self, phase: str, context: object) -> dict[str, object]:
                result = super().snapshot(phase, context)
                if phase == "rollback":
                    result["authority_revision"] = "different-authority"
                return result

        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.json"
            engine = UpgradeEngine(
                "op-mismatched",
                journal,
                {**CONTEXT, "operation_id": "op-mismatched"},
                backend_adapter=MismatchedAdapter(),
            )
            engine.plan()
            with self.assertRaises(UpgradeError):
                engine.rollback(lambda _step, _state: {})
            self.assertEqual([], json.loads(journal.read_text())["records"])

    def test_rollback_handler_cannot_override_adapter_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.json"
            engine = UpgradeEngine(
                "op-handler-evidence",
                journal,
                {**CONTEXT, "operation_id": "op-handler-evidence"},
                backend_adapter=FakeAdapter(),
            )
            engine.plan()
            value = json.loads(journal.read_text())
            value["status"] = "failed"
            value["phase"] = "discover"
            value["records"] = [
                {
                    "operation_id": "op-handler-evidence",
                    "step_id": "op-handler-evidence.discover",
                    "phase": "discover",
                    "outcome": "failed",
                    "error": "failed",
                    "context": {**CONTEXT, "operation_id": "op-handler-evidence"},
                }
            ]
            journal.write_text(json.dumps(value))

            def forge(_step: str, _state: Mapping[str, object]) -> dict[str, object]:
                return {"restored_verified": False}

            with self.assertRaises(UpgradeError):
                engine.rollback(forge)
            self.assertEqual("safe-mode", engine._load()["status"])

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
                {
                    "operation_id": "op-5",
                    "step_id": "op-5.discover",
                    "phase": "discover",
                    "outcome": "started",
                    "context": {**CONTEXT, "operation_id": "op-5"},
                }
            ]
            journal.write_text(json.dumps(value))
            with self.assertRaises(UpgradeError):
                engine.apply({})

    def test_authority_probe_failure_precedes_handler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = UpgradeEngine(
                "op-probe",
                Path(directory) / "journal.json",
                {**CONTEXT, "operation_id": "op-probe"},
                backend_adapter=FailingAdapter(),
            )
            engine.plan()
            called = False

            def handler(_operation: str, _state: object) -> dict[str, object]:
                nonlocal called
                called = True
                return {"mutates_authority": False}

            with self.assertRaises(UpgradeError):
                engine.apply(dict.fromkeys(PHASES, handler))
            self.assertFalse(called)


if __name__ == "__main__":
    unittest.main()
