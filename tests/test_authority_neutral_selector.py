# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile tests for selector publication admission evidence."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from tools.authority_neutral_selector import (
    BoundSelectorAdmissionAdapter,
    SelectorAdmissionExecutionError,
    execute_verified_selector_admission,
)

OPERATION = {"operation_id": "op-1:selector", "opcode": "selector.admit"}
CONTEXT = {
    "backend": "sqlite",
    "target": "new",
    "operation_id": "op-1",
    "fencing_token": "fence-1",
    "selector_ref": "runtime-selector.json",
    "expected_state_revision": 7,
    "durable_barrier_id": "barrier-1",
    "selector_root": "/runtime",
    "binding": {"project_id": "project-1"},
}


class _Backend:
    def __init__(self, snapshot: object = object()) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[object, ...]] = []

    class _Scope:
        def __init__(self, owner: _Backend) -> None:
            self.owner = owner

        def __enter__(self) -> object:
            return self.owner.snapshot

        def __exit__(self, *_args: object) -> None:
            return None

    def selector_visibility_scope(self, *args: object, **kwargs: object) -> _Scope:
        self.calls.append((*args, kwargs))
        return self._Scope(self)


class SelectorAdmissionTests(unittest.TestCase):
    def test_admission_is_read_only_and_bound_to_visibility_scope(self) -> None:
        backend = _Backend()
        result = execute_verified_selector_admission(backend, OPERATION, CONTEXT)
        self.assertTrue(result["selector_admission_verified"])
        self.assertFalse(result["mutates_authority"])
        self.assertFalse(result["selector_commit_atomic"])
        self.assertEqual(1, len(backend.calls))

    def test_rejects_forged_operation_context_and_missing_snapshot(self) -> None:
        with self.assertRaisesRegex(SelectorAdmissionExecutionError, "unsupported"):
            execute_verified_selector_admission(_Backend(), {"opcode": "selector.commit"}, CONTEXT)
        for field in ("selector_root", "binding"):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(SelectorAdmissionExecutionError, "incomplete"),
            ):
                value = dict(CONTEXT)
                value.pop(field)
                execute_verified_selector_admission(_Backend(), OPERATION, value)
        with self.assertRaisesRegex(SelectorAdmissionExecutionError, "absent"):
            execute_verified_selector_admission(_Backend(None), OPERATION, CONTEXT)

    def test_rejects_invalid_identity_and_backend_failures(self) -> None:
        for field, value, message in (
            ("backend", "git", "identity"),
            ("target", "rollback", "identity"),
            ("operation_id", "", "operation identity"),
            ("fencing_token", "", "fencing identity"),
            ("expected_state_revision", True, "revision"),
            ("durable_barrier_id", "", "barrier identity"),
            ("selector_ref", "", "selector identity"),
            ("selector_root", "", "root"),
            ("binding", [], "binding"),
        ):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(SelectorAdmissionExecutionError, message),
            ):
                execute_verified_selector_admission(
                    _Backend(), OPERATION, {**CONTEXT, field: value}
                )
        backend = _Backend()
        with (
            patch.object(backend, "selector_visibility_scope", side_effect=OSError("drift")),
            self.assertRaisesRegex(SelectorAdmissionExecutionError, "recheck failed"),
        ):
            execute_verified_selector_admission(backend, OPERATION, CONTEXT)

    def test_bound_adapter_rejects_phase_and_identity_drift(self) -> None:
        backend = _Backend()
        with self.assertRaisesRegex(SelectorAdmissionExecutionError, "unsupported"):
            BoundSelectorAdmissionAdapter(backend, {"opcode": "selector.commit"}, CONTEXT)
        adapter = BoundSelectorAdmissionAdapter(backend, OPERATION, CONTEXT)
        result = adapter.execute("selector_admission", CONTEXT)
        self.assertTrue(result["selector_admission_verified"])
        with self.assertRaisesRegex(SelectorAdmissionExecutionError, "unsupported"):
            adapter.execute("commit", CONTEXT)
        with self.assertRaisesRegex(SelectorAdmissionExecutionError, "identity mismatch"):
            adapter.execute("selector_admission", {**CONTEXT, "fencing_token": "foreign"})


if __name__ == "__main__":
    unittest.main()
