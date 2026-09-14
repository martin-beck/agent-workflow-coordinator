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

from tools.rollback_control_store import (
    SQLiteRollbackControlStore,
    bind_control_store,
)
from tools.upgrade_admission import (
    PREFLIGHT_PREDICATES,
    QUIESCENCE_PREDICATES,
    REOPEN_PREDICATES,
)
from tools.upgrade_engine import CONTEXT_FIELDS, PHASES, Handler, UpgradeEngine, UpgradeError
from tools.upgrade_identity import canonical_barrier_digest, canonical_envelope_digest


def make_context(operation_id: str = "op-1", target: str = "new") -> dict[str, object]:
    context: dict[str, object] = {
        "schema_version": 2,
        "backend": "sqlite",
        "project_id": "11111111-1111-4111-8111-111111111111",
        "operation_id": operation_id,
        "state_revision": 1,
        "authority_revision": "authority-1",
        "fencing_token": "fence-1",
        "fencing_owner": "worker-1",
        "durable_barrier_id": "barrier-1",
        "artifact_root": "/artifacts",
        "source": "/authority.sqlite",
        "destination": "/artifacts/backup.sqlite",
        "manifest": "/artifacts/manifest.json",
        "barrier_identity_digest": "0" * 64,
        "target": target,
        "envelope_digest": "0" * 64,
    }
    context["barrier_identity_digest"] = canonical_barrier_digest(context)
    context["envelope_digest"] = canonical_envelope_digest(context)
    return context


CONTEXT = make_context()
ROLLBACK_CONTEXT = make_context(target="rollback")
ADMISSION = {
    **dict.fromkeys(PREFLIGHT_PREDICATES, True),
    **dict.fromkeys(QUIESCENCE_PREDICATES, True),
    **dict.fromkeys(REOPEN_PREDICATES, True),
    **CONTEXT,
    "validation_failed": False,
}


class FakeAdapter:
    def verify_rollback_context(self, context: Mapping[str, object]) -> dict[str, object] | None:
        expected = make_context(str(context.get("operation_id")), target="rollback")
        if all(context.get(field) == expected[field] for field in CONTEXT_FIELDS):
            return dict(context)
        return None

    def snapshot(self, phase: str, context: object) -> dict[str, object]:
        identity = dict(context) if isinstance(context, Mapping) else CONTEXT
        if phase == "rollback":
            return {
                **make_context(str(identity["operation_id"]), target="rollback"),
                "rollback_context_verified": True,
            }
        return {
            **ADMISSION,
            **{field: identity[field] for field in CONTEXT_FIELDS},
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

    def release_rollback_context(self, _context: Mapping[str, object]) -> dict[str, object]:
        return {"status": "released"}

    def revalidate_rollback(
        self, _context: Mapping[str, object], result: Mapping[str, object]
    ) -> dict[str, object]:
        return dict(result)


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
                make_context("op-2"),
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
                make_context("op-evidence"),
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
                "op-3", journal, make_context("op-3"), backend_adapter=FakeAdapter()
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
                "op-4", journal, make_context("op-4"), backend_adapter=FakeAdapter()
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
                make_context("op-record"),
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
                    "context": make_context("op-record"),
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
                make_context("op-rollback"),
                backend_adapter=FakeAdapter(),
            )
            engine.plan()
            base = json.loads(journal.read_text())
            record = {
                "operation_id": "op-rollback",
                "step_id": "op-rollback.rollback",
                "phase": "rollback",
                "outcome": "started",
                "context": make_context("op-rollback", target="rollback"),
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
                make_context("op-success-rollback"),
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
                    "context": make_context("op-success-rollback"),
                }
            ]
            journal.write_text(json.dumps(value))
            result = engine.rollback(lambda _step, _state: {})
            self.assertEqual("rolled-back", result["status"])
            self.assertEqual("rollback_completed", result["records"][-1]["outcome"])

            reloaded = UpgradeEngine(
                "op-success-rollback",
                journal,
                make_context("op-success-rollback"),
                backend_adapter=FakeAdapter(),
            )
            self.assertEqual("rollback", reloaded._load()["phase"])

            tampered = json.loads(journal.read_text())
            tampered["records"][-1]["context"]["envelope_digest"] = "e" * 64
            journal.write_text(json.dumps(tampered))
            with self.assertRaises(UpgradeError):
                reloaded._load()

            tampered = json.loads(journal.read_text())
            tampered["records"][-1]["context"]["envelope_digest"] = ROLLBACK_CONTEXT[
                "envelope_digest"
            ]
            tampered["rollback_verified"] = False
            journal.write_text(json.dumps(tampered))
            with self.assertRaises(UpgradeError):
                reloaded._load()

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
                make_context("op-unverified"),
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
                make_context("op-mismatched"),
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
                make_context("op-handler-evidence"),
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
                    "context": make_context("op-handler-evidence"),
                }
            ]
            journal.write_text(json.dumps(value))

            def forge(_step: str, _state: Mapping[str, object]) -> dict[str, object]:
                return {"restored_verified": False}

            with self.assertRaises(UpgradeError):
                engine.rollback(forge)
            self.assertEqual("safe-mode", engine._load()["status"])

    def test_rollback_uses_durable_sqlite_control_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            operation_id = "op-control-store"
            authority = Path(directory) / "authority.sqlite"
            authority.touch()
            control = SQLiteRollbackControlStore(
                Path(directory) / "control.sqlite",
                cast(str, CONTEXT["project_id"]),
                authority,
            )
            control_record = {
                **ROLLBACK_CONTEXT,
                "operation_id": operation_id,
                "status": "held",
                "revision": 1,
            }
            control_record["barrier_identity_digest"] = canonical_barrier_digest(control_record)
            control_record["envelope_digest"] = canonical_envelope_digest(control_record)
            control.cas(0, control_record)

            lock_observations: list[bool] = []

            class ControlDelegate(FakeAdapter):
                def snapshot(self, phase: str, context: object) -> dict[str, object]:
                    lock_observations.append(control.operation_owned_by_current_thread)
                    if phase == "rollback":
                        return {**control_record, "rollback_context_verified": True}
                    return super().snapshot(phase, context)

                def execute(self, phase: str, context: object) -> dict[str, object]:
                    lock_observations.append(control.operation_owned_by_current_thread)
                    return super().execute(phase, context)

            adapter = bind_control_store("sqlite", ControlDelegate(), control)

            journal = Path(directory) / "journal.json"
            engine = UpgradeEngine(
                operation_id,
                journal,
                make_context(operation_id),
                backend_adapter=adapter,
            )
            engine.plan()
            value = json.loads(journal.read_text())
            value["status"] = "failed"
            value["phase"] = "discover"
            value["records"] = [
                {
                    "operation_id": operation_id,
                    "step_id": f"{operation_id}.discover",
                    "phase": "discover",
                    "outcome": "failed",
                    "error": "failed",
                    "context": make_context(operation_id),
                }
            ]
            journal.write_text(json.dumps(value))
            result = engine.rollback(lambda _step, _state: {})
            self.assertEqual("rolled-back", result["status"])
            self.assertEqual("released", control.snapshot(operation_id)["status"])
            self.assertTrue(lock_observations)
            self.assertTrue(all(lock_observations))
            reloaded = UpgradeEngine(
                operation_id,
                journal,
                make_context(operation_id),
                backend_adapter=adapter,
            )
            self.assertEqual("rolled-back", reloaded._load()["status"])

    def test_started_phase_requires_explicit_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.json"
            engine = UpgradeEngine(
                "op-5", journal, make_context("op-5"), backend_adapter=FakeAdapter()
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
                    "context": make_context("op-5"),
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
                make_context("op-probe"),
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
