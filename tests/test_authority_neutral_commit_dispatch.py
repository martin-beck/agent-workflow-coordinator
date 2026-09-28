# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile tests for the isolated generated commit-phase adapter."""

from __future__ import annotations

import unittest
from typing import cast

from tools.authority_mutation import MutationReceipt
from tools.authority_neutral_commit_dispatch import (
    BoundCommitPhaseAdapter,
    CommitDispatchError,
)

OPERATION = {
    "operation_id": "upgrade-1:commit",
    "opcode": "authority.atomic_replace",
    "inputs": {
        "backend": "git",
        "selector_ref": ".runtime/runtime-selector.json",
        "expected_state_revision": 7,
        "barrier_id": "barrier-7",
        "fencing_token": "fence-7",
        "backup_operation_id": "upgrade-1:backup",
    },
    "timeout_seconds": 300,
    "resources": ["maintenance-barrier", "durable-operation-record"],
    "preconditions": ["previous-phase-complete"],
    "postconditions": ["commit-contract-satisfied"],
    "evidence": ["durable-operation-record"],
    "durable_record": "operation-id-and-outcome",
}
CONTEXT = {
    "backend": "git",
    "target": "new",
    "operation_id": "upgrade-1:commit",
    "state_revision": 7,
    "durable_barrier_id": "barrier-7",
    "fencing_token": "fence-7",
}


class FakeExecutor:
    def __init__(self, receipt: MutationReceipt) -> None:
        self.receipt = receipt
        self.arguments: list[object] = []

    def execute(self, argument: object) -> MutationReceipt:
        self.arguments.append(argument)
        return self.receipt


def receipt(
    *, operation_id: str = "upgrade-1:commit", mutates_authority: bool = True
) -> MutationReceipt:
    return MutationReceipt(
        backend="git",
        target="new",
        operation_id=operation_id,
        state_revision=7,
        barrier_id="barrier-7",
        artifact_identity="artifact",
        manifest_identity="manifest",
        selector_identity="selector",
        runtime_identity="runtime",
        fencing_token="fence-7",  # noqa: S106
        mutates_authority=mutates_authority,
    )


class CommitDispatchTests(unittest.TestCase):
    def test_bound_adapter_dispatches_one_exact_generated_commit(self) -> None:
        executor = FakeExecutor(receipt())
        adapter = BoundCommitPhaseAdapter(OPERATION, CONTEXT, executor, "built-in-effect")
        result = adapter.execute("commit", dict(CONTEXT))
        self.assertTrue(result["authority_effect_verified"])
        self.assertTrue(result["mutates_authority"])
        self.assertEqual(["built-in-effect"], executor.arguments)
        with self.assertRaisesRegex(CommitDispatchError, "already consumed"):
            adapter.execute("commit", dict(CONTEXT))
        self.assertEqual(["built-in-effect"], executor.arguments)

    def test_rejects_phase_identity_and_receipt_drift(self) -> None:
        executor = FakeExecutor(receipt())
        adapter = BoundCommitPhaseAdapter(OPERATION, CONTEXT, executor, object())
        with self.assertRaisesRegex(CommitDispatchError, "phase"):
            adapter.execute("apply", CONTEXT)
        with self.assertRaisesRegex(CommitDispatchError, "identity drifted"):
            adapter.execute("commit", {**CONTEXT, "fencing_token": "foreign"})
        with self.assertRaisesRegex(CommitDispatchError, "receipt identity"):
            BoundCommitPhaseAdapter(
                OPERATION,
                CONTEXT,
                FakeExecutor(receipt(operation_id="foreign")),
                object(),
            ).execute("commit", CONTEXT)

    def test_rejects_non_atomic_or_malformed_operation(self) -> None:
        with self.assertRaisesRegex(CommitDispatchError, "opcode"):
            BoundCommitPhaseAdapter(
                {**OPERATION, "opcode": "backend.restore"},
                CONTEXT,
                FakeExecutor(receipt()),
                object(),
            )
        malformed = dict(OPERATION)
        malformed["inputs"] = dict(
            cast(dict[str, object], OPERATION["inputs"]), backup_operation_id="wrong"
        )
        with self.assertRaisesRegex(CommitDispatchError, "backup identity"):
            BoundCommitPhaseAdapter(malformed, CONTEXT, FakeExecutor(receipt()), object())


if __name__ == "__main__":
    unittest.main()
