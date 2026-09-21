# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile tests for diagnostic recovery evidence."""

from __future__ import annotations

import unittest

from tools.authority_neutral_recovery import (
    BoundRecoveryEvidenceAdapter,
    RecoveryAuthorizationExecutionError,
    execute_verified_recovery_evidence,
)

OPERATION = {"operation_id": "op-1:rollback", "opcode": "rollback.admit"}
EVIDENCE = {
    "backend": "sqlite",
    "target": "rollback",
    "operation_id": "op-1",
    "fencing_token": "fence-1",
    "state_revision": 7,
    "barrier_id": "barrier-1",
    "backup_verified": True,
    "restore_roundtrip_verified": True,
    "runtime_validated": True,
    "backend_identity_verified": True,
    "mutates_authority": False,
    "rollback_context_verified": False,
}


class RecoveryEvidenceTests(unittest.TestCase):
    def test_diagnostic_recovery_evidence_never_authorizes_rollback(self) -> None:
        result = execute_verified_recovery_evidence(OPERATION, EVIDENCE)
        self.assertTrue(result["recovery_evidence_verified"])
        self.assertFalse(result["rollback_authorized"])
        self.assertFalse(result["mutates_authority"])
        self.assertFalse(result["rollback_context_verified"])

    def test_rejects_forged_or_unverified_recovery(self) -> None:
        with self.assertRaisesRegex(RecoveryAuthorizationExecutionError, "unsupported"):
            execute_verified_recovery_evidence({"opcode": "rollback"}, EVIDENCE)
        for field, value, message in (
            ("target", "new", "identity"),
            ("backup_verified", False, "unverified"),
            ("runtime_validated", False, "unverified"),
            ("mutates_authority", True, "mutating"),
            ("rollback_context_verified", True, "authorizing"),
            ("state_revision", 0, "revision"),
        ):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(RecoveryAuthorizationExecutionError, message),
            ):
                execute_verified_recovery_evidence(OPERATION, {**EVIDENCE, field: value})
        incomplete = dict(EVIDENCE)
        incomplete.pop("restore_roundtrip_verified")
        with self.assertRaisesRegex(RecoveryAuthorizationExecutionError, "incomplete"):
            execute_verified_recovery_evidence(OPERATION, incomplete)

    def test_bound_adapter_rejects_phase_and_identity_drift(self) -> None:
        with self.assertRaisesRegex(RecoveryAuthorizationExecutionError, "unsupported"):
            BoundRecoveryEvidenceAdapter({"opcode": "rollback"}, EVIDENCE)
        adapter = BoundRecoveryEvidenceAdapter(OPERATION, EVIDENCE)
        result = adapter.execute("recovery_admission", EVIDENCE)
        self.assertTrue(result["recovery_evidence_verified"])
        with self.assertRaisesRegex(RecoveryAuthorizationExecutionError, "unsupported"):
            adapter.execute("rollback", EVIDENCE)
        with self.assertRaisesRegex(RecoveryAuthorizationExecutionError, "identity mismatch"):
            adapter.execute("recovery_admission", {**EVIDENCE, "fencing_token": "foreign"})


if __name__ == "__main__":
    unittest.main()
