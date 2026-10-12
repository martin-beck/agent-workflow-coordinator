# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Contract coverage for execution-envelope intent records."""

from __future__ import annotations

import unittest
import uuid

from tools.execution_envelope import (
    ExecutionEnvelopeError,
    append_record,
    argv_digest,
    load_records,
    validate_envelope,
    validate_result,
)


def envelope(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "execution_id": str(uuid.UUID(int=1)),
        "task": "AR-0092",
        "task_revision": 7,
        "owner": "worker-a",
        "claim_expires": "2026-10-12T12:00:00+00:00",
        "worktree_key": "worktree-a",
        "branch": "feature/a",
        "spec_ref": None,
        "spec_revision": None,
        "policy_digest": "a" * 64,
        "argv_sha256": argv_digest(["/bin/true"]),
        "timeout_seconds": 30.0,
        "created_at": "2026-10-12T11:00:00+00:00",
    }
    value.update(changes)
    return value


class ExecutionEnvelopeTests(unittest.TestCase):
    def test_accepts_exact_bound_intent(self) -> None:
        self.assertEqual("AR-0092", validate_envelope(envelope())["task"])

    def test_rejects_raw_argv_and_partial_spec_binding(self) -> None:
        with self.assertRaisesRegex(ExecutionEnvelopeError, "fields"):
            validate_envelope({**envelope(), "argv": ["secret"]})
        with self.assertRaisesRegex(ExecutionEnvelopeError, "spec binding"):
            validate_envelope(envelope(spec_ref="spec.json", spec_revision=None))

    def test_argv_digest_does_not_retain_arguments(self) -> None:
        argument = "private-fixture-value"
        digest = argv_digest(["command", argument])
        self.assertEqual(64, len(digest))
        self.assertNotIn(argument, digest)

    def test_results_are_exact_and_pair_with_an_immutable_intent(self) -> None:
        result = validate_result(
            {
                "schema_version": 1,
                "execution_id": str(uuid.UUID(int=1)),
                "classification": "EXIT",
                "returncode": 0,
                "recorded_at": "2026-10-12T11:01:00+00:00",
            }
        )
        self.assertEqual("EXIT", result["classification"])
        with self.assertRaisesRegex(ExecutionEnvelopeError, "classification"):
            validate_result({**result, "classification": "UNKNOWN"})

    def test_append_preserves_prior_intent_and_result(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory

        result = {
            "schema_version": 1,
            "execution_id": str(uuid.UUID(int=1)),
            "classification": "EXIT",
            "returncode": 0,
            "recorded_at": "2026-10-12T11:01:00+00:00",
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            append_record(root, "AR-0092", envelope())
            append_record(root, "AR-0092", result)
            self.assertEqual([envelope(), result], load_records(root, "AR-0092"))


if __name__ == "__main__":
    unittest.main()
