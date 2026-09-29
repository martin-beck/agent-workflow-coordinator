# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from tools.authority_mutation import DurableBoundBackendMutation
from tools.authority_neutral_commit import CommitAdmissionBundle
from tools.production_phase_effects import (
    ProductionPhaseEffectError,
    _operation_inputs,
    _validate_admission,
    bind_production_phase_effects,
)
from tools.production_phase_engine import ProductionPhaseBinding


class _Journal:
    def prepare_authority_effect(self, *_args: object, **_kwargs: object) -> object:
        return object()

    def finish_authority_effect(
        self, _intent: object, _outcome: str, _receipt: object | None = None
    ) -> None:
        return None


def _admission(target: str) -> CommitAdmissionBundle:
    suffix = "commit" if target == "new" else "rollback"
    return CommitAdmissionBundle(
        backend="sqlite",
        target=target,
        operation_id=f"phase-effect-test:{suffix}",
        fencing_token="fence-test",  # noqa: S106
        state_revision=3,
        barrier_id="barrier-test",
        artifact_identity="artifact-test",
        manifest_identity="manifest-test",
        selector_identity="selector-test",
        runtime_identity="runtime-test",
    )


def _operation(target: str) -> dict[str, object]:
    suffix = "commit" if target == "new" else "rollback"
    inputs: dict[str, object] = {
        "backend": "sqlite",
        "selector_ref": ".runtime/runtime-selector.json",
        "expected_state_revision": 3,
        "barrier_id": "barrier-test",
        "fencing_token": "fence-test",
        "backup_operation_id": "phase-effect-test:backup",
    }
    if target == "rollback":
        inputs["target"] = target
    return {
        "operation_id": f"phase-effect-test:{suffix}",
        "opcode": "authority.atomic_replace" if target == "new" else "backend.restore",
        "inputs": inputs,
        "timeout_seconds": 300,
        "resources": ["maintenance-barrier"],
        "preconditions": ["previous-phase-complete"],
        "postconditions": ["phase-contract-satisfied"],
        "evidence": ["durable-operation-record"],
        "durable_record": "operation-id-and-outcome",
    }


def _binding() -> ProductionPhaseBinding:
    binding = object.__new__(ProductionPhaseBinding)
    object.__setattr__(
        binding,
        "operations",
        {"commit": _operation("new"), "rollback": _operation("rollback")},
    )
    object.__setattr__(binding, "live_binding", object())
    return binding


def _capability(admission: CommitAdmissionBundle) -> DurableBoundBackendMutation:
    def effect(_argument: object) -> dict[str, object]:
        return {
            "backend": admission.backend,
            "target": admission.target,
            "operation_id": admission.operation_id,
            "state_revision": admission.state_revision,
            "barrier_id": admission.barrier_id,
            "artifact_identity": admission.artifact_identity,
            "manifest_identity": admission.manifest_identity,
            "selector_identity": admission.selector_identity,
            "runtime_identity": admission.runtime_identity,
            "fencing_token": admission.fencing_token,
            "mutates_authority": True,
        }

    return DurableBoundBackendMutation(
        admission, _Journal(), session_revision=3, backend_effect=effect
    )


class ProductionPhaseEffectsTests(unittest.TestCase):
    def test_composes_and_consumes_exact_commit_and_rollback_capabilities(self) -> None:
        commit = _admission("new")
        rollback = _admission("rollback")
        with (
            patch(
                "tools.production_phase_effects.bind_durable_commit_capability",
                return_value=_capability(commit),
            ),
            patch(
                "tools.production_phase_effects.bind_durable_rollback_capability",
                return_value=_capability(rollback),
            ),
        ):
            effects = bind_production_phase_effects(
                _binding(),
                _Journal(),
                commit,
                rollback,
                session_revision=3,
                admission_reread=dict,
                commit_argument="new-state",
                rollback_argument="backup-state",
                rollback_effect=lambda _argument: {},
            )

        context = {
            "backend": "sqlite",
            "target": "new",
            "operation_id": "phase-effect-test:commit",
            "state_revision": 3,
            "durable_barrier_id": "barrier-test",
            "fencing_token": "fence-test",
        }
        result = effects.commit.execute("commit", context)
        self.assertTrue(result["authority_effect_verified"])
        rollback_result = effects.rollback.execute(
            {
                **context,
                "target": "rollback",
                "selector_ref": ".runtime/runtime-selector.json",
                "operation_id": "phase-effect-test:rollback",
            }
        )
        self.assertTrue(rollback_result["restored_verified"])

    def test_rejects_foreign_admission_before_binding_capabilities(self) -> None:
        commit = _admission("new")
        foreign = CommitAdmissionBundle(
            backend="sqlite",
            target="new",
            operation_id=commit.operation_id,
            fencing_token="foreign-fence",  # noqa: S106
            state_revision=3,
            barrier_id=commit.barrier_id,
            artifact_identity=commit.artifact_identity,
            manifest_identity=commit.manifest_identity,
            selector_identity=commit.selector_identity,
            runtime_identity=commit.runtime_identity,
        )
        with (
            patch("tools.production_phase_effects.bind_durable_commit_capability") as commit_bind,
            self.assertRaises(ProductionPhaseEffectError),
        ):
            bind_production_phase_effects(
                _binding(),
                Path("journal"),
                foreign,
                _admission("rollback"),
                session_revision=3,
                admission_reread=dict,
                commit_argument=object(),
                rollback_argument=object(),
                rollback_effect=lambda _argument: {},
            )
        commit_bind.assert_not_called()

    def test_rejects_malformed_generated_operation_inputs(self) -> None:
        operation = _operation("new")
        base_inputs = cast(dict[str, object], operation["inputs"])
        cases: tuple[dict[str, object], ...] = (
            {**operation, "inputs": None},
            {**operation, "inputs": {"backend": "sqlite"}},
            {
                **operation,
                "inputs": {**base_inputs, "target": "rollback"},
            },
            {
                **operation,
                "inputs": {**base_inputs, "backend": "foreign"},
            },
            {
                **operation,
                "inputs": {**base_inputs, "expected_state_revision": True},
            },
            {
                **operation,
                "inputs": {**base_inputs, "selector_ref": ""},
            },
        )
        for candidate in cases:
            with self.assertRaises(ProductionPhaseEffectError):
                _operation_inputs(candidate, target="new")

        rollback = _operation("rollback")
        rollback_inputs = dict(cast(dict[str, object], rollback["inputs"]))
        del rollback_inputs["target"]
        with self.assertRaises(ProductionPhaseEffectError):
            _operation_inputs({**rollback, "inputs": rollback_inputs}, target="rollback")

    def test_rejects_malformed_operation_and_admission_types(self) -> None:
        operation = _operation("new")
        with self.assertRaises(ProductionPhaseEffectError):
            _validate_admission(
                {**operation, "operation_id": "foreign"},
                _admission("new"),
                target="new",
                suffix="commit",
            )
        with self.assertRaises(ProductionPhaseEffectError):
            _validate_admission(
                operation,
                object(),  # type: ignore[arg-type]
                target="new",
                suffix="commit",
            )

    def test_rejects_ambiguous_or_invalid_rollback_configuration(self) -> None:
        commit = _admission("new")
        rollback = _admission("rollback")
        cases = (
            {},
            {"rollback_effect": lambda _argument: {}, "rollback_backup": Path("backup")},
            {"rollback_effect": "not-callable"},
        )
        for options in cases:
            with self.subTest(options=options), self.assertRaises(ProductionPhaseEffectError):
                bind_production_phase_effects(
                    _binding(),
                    _Journal(),
                    commit,
                    rollback,
                    session_revision=3,
                    admission_reread=dict,
                    commit_argument=object(),
                    rollback_argument=object(),
                    **options,
                )

    def test_composes_concrete_rollback_backup_branch(self) -> None:
        commit = _admission("new")
        rollback = _admission("rollback")
        with (
            patch(
                "tools.production_phase_effects.bind_durable_commit_capability",
                return_value=_capability(commit),
            ),
            patch(
                "tools.production_phase_effects.bind_concrete_durable_rollback_capability",
                return_value=_capability(rollback),
            ) as rollback_bind,
        ):
            effects = bind_production_phase_effects(
                _binding(),
                _Journal(),
                commit,
                rollback,
                session_revision=3,
                admission_reread=dict,
                commit_argument="new-state",
                rollback_argument="ignored-by-concrete-backup",
                rollback_backup=Path("backup"),
                expected_branch="main",
                expected_head="a" * 40,
            )
        rollback_bind.assert_called_once()
        result = effects.rollback.execute(
            {
                "backend": "sqlite",
                "target": "rollback",
                "operation_id": "phase-effect-test:rollback",
                "state_revision": 3,
                "durable_barrier_id": "barrier-test",
                "fencing_token": "fence-test",
                "selector_ref": ".runtime/runtime-selector.json",
            }
        )
        self.assertTrue(result["restored_verified"])

    def test_rejects_invalid_binding_callbacks_and_missing_operations(self) -> None:
        with self.assertRaises(ProductionPhaseEffectError):
            bind_production_phase_effects(
                object(),  # type: ignore[arg-type]
                _Journal(),
                _admission("new"),
                _admission("rollback"),
                session_revision=3,
                admission_reread=dict,
                commit_argument=object(),
                rollback_argument=object(),
                rollback_effect=lambda _argument: {},
            )
        incomplete = object.__new__(ProductionPhaseBinding)
        object.__setattr__(incomplete, "operations", {})
        object.__setattr__(incomplete, "live_binding", object())
        with self.assertRaises(ProductionPhaseEffectError):
            bind_production_phase_effects(
                incomplete,
                _Journal(),
                _admission("new"),
                _admission("rollback"),
                session_revision=3,
                admission_reread=None,  # type: ignore[arg-type]
                commit_argument=object(),
                rollback_argument=object(),
                rollback_effect=lambda _argument: {},
            )

    def test_wraps_capability_binding_failures_and_preserves_typed_errors(self) -> None:
        with (
            patch(
                "tools.production_phase_effects.bind_durable_commit_capability",
                side_effect=RuntimeError("backend rejected"),
            ),
            self.assertRaisesRegex(ProductionPhaseEffectError, "effects were rejected"),
        ):
            bind_production_phase_effects(
                _binding(),
                _Journal(),
                _admission("new"),
                _admission("rollback"),
                session_revision=3,
                admission_reread=dict,
                commit_argument=object(),
                rollback_argument=object(),
                rollback_effect=lambda _argument: {},
            )
        typed = ProductionPhaseEffectError("typed rejection")
        with (
            patch(
                "tools.production_phase_effects.bind_durable_commit_capability",
                side_effect=typed,
            ),
            self.assertRaisesRegex(ProductionPhaseEffectError, "typed rejection"),
        ):
            bind_production_phase_effects(
                _binding(),
                _Journal(),
                _admission("new"),
                _admission("rollback"),
                session_revision=3,
                admission_reread=dict,
                commit_argument=object(),
                rollback_argument=object(),
                rollback_effect=lambda _argument: {},
            )


if __name__ == "__main__":
    unittest.main()
