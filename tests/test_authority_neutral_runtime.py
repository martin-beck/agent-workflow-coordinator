# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile tests for runtime replacement admission evidence."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from tools.authority_neutral_runtime import (
    BoundRuntimeAdmissionAdapter,
    RuntimeAdmissionExecutionError,
    execute_verified_runtime_admission,
)

OPERATION = {"operation_id": "op-1:runtime", "opcode": "runtime.replace.admit"}
CONTEXT = {
    "backend": "sqlite",
    "target": "new",
    "operation_id": "op-1",
    "fencing_token": "fence-1",
    "runtime_root": "/artifacts/runtime",
    "manifest_digest": "a" * 64,
    "selector_root": "/runtime",
    "selector_ref": "runtime-selector.json",
    "expected_release": "v0.3.6",
    "binding": {"project_id": "project-1"},
}


class RuntimeAdmissionTests(unittest.TestCase):
    def test_admission_verifies_runtime_and_selector_without_mutation(self) -> None:
        with (
            patch("tools.authority_neutral_runtime.verify_runtime_manifest", return_value=True),
            patch(
                "tools.authority_neutral_runtime.read_runtime_selector",
                return_value={"active_release": "v0.3.6"},
            ),
        ):
            result = execute_verified_runtime_admission(OPERATION, CONTEXT)
        self.assertTrue(result["runtime_replacement_admission_verified"])
        self.assertTrue(result["selector_verified"])
        self.assertFalse(result["mutates_authority"])
        self.assertFalse(result["replacement_atomic"])

    def test_rejects_forged_context_manifest_selector_and_backend_failures(self) -> None:
        with self.assertRaisesRegex(RuntimeAdmissionExecutionError, "unsupported"):
            execute_verified_runtime_admission({"opcode": "runtime.replace"}, CONTEXT)
        for field, value in (("runtime_root", ""), ("expected_release", ""), ("binding", [])):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(RuntimeAdmissionExecutionError, field.split("_")[0]),
            ):
                execute_verified_runtime_admission(OPERATION, {**CONTEXT, field: value})
        with (
            patch("tools.authority_neutral_runtime.verify_runtime_manifest", return_value=False),
            self.assertRaisesRegex(RuntimeAdmissionExecutionError, "incomplete"),
        ):
            execute_verified_runtime_admission(OPERATION, CONTEXT)
        with (
            patch("tools.authority_neutral_runtime.verify_runtime_manifest", return_value=True),
            patch(
                "tools.authority_neutral_runtime.read_runtime_selector",
                return_value={"active_release": "foreign"},
            ),
            self.assertRaisesRegex(RuntimeAdmissionExecutionError, "release identity"),
        ):
            execute_verified_runtime_admission(OPERATION, CONTEXT)

    def test_bound_adapter_rejects_phase_and_identity_drift(self) -> None:
        adapter = BoundRuntimeAdmissionAdapter(OPERATION, CONTEXT)
        with (
            patch("tools.authority_neutral_runtime.verify_runtime_manifest", return_value=True),
            patch(
                "tools.authority_neutral_runtime.read_runtime_selector",
                return_value={"active_release": "v0.3.6"},
            ),
        ):
            result = adapter.execute("runtime_admission", CONTEXT)
        self.assertTrue(result["runtime_replacement_admission_verified"])
        with self.assertRaisesRegex(RuntimeAdmissionExecutionError, "unsupported"):
            adapter.execute("commit", CONTEXT)
        with self.assertRaisesRegex(RuntimeAdmissionExecutionError, "identity mismatch"):
            adapter.execute("runtime_admission", {**CONTEXT, "fencing_token": "foreign"})


if __name__ == "__main__":
    unittest.main()
