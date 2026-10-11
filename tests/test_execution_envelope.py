# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Contract tests for immutable, non-authorizing execution envelopes."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.execution_envelope import digest, errors, preflight


def envelope() -> dict[str, object]:
    return {
        "schema_version": 1,
        "envelope_id": "AR-0001-e1",
        "envelope_revision": 1,
        "task": "AR-0001",
        "task_revision": 7,
        "spec_ref": "specs/AR-0001.json",
        "spec_revision": 2,
        "outputs": [{"id": "implementation"}],
        "non_goals": ["Do not authorize by prompt."],
        "path_scope": {"read": ["src", "tests"], "write": ["src", "tests"]},
        "command_classes": {
            "read": {"allowed": True},
            "test": {"allowed": True, "timeout_seconds": 60},
            "source-write": {"allowed": True, "timeout_seconds": 120},
            "git-publication": {"allowed": False},
        },
        "evidence": [{"id": "tests"}],
        "stop_conditions": [{"id": "scope", "kind": "deny"}],
    }


class ExecutionEnvelopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "envelopes").mkdir()
        self.worktree = self.root / "worker"
        (self.worktree / "src").mkdir(parents=True)
        self.reference = "envelopes/AR-0001-e1.json"
        self.write_envelope(envelope())

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_envelope(self, value: dict[str, object]) -> None:
        self.payload = json.dumps(value, sort_keys=True).encode()
        (self.root / self.reference).write_bytes(self.payload)

    def meta(self) -> dict[str, object]:
        return {
            "id": "AR-0001",
            "owner": "worker",
            "status": "in_progress",
            "spec_ref": "specs/AR-0001.json",
            "spec_revision": 2,
            "execution_envelope_ref": self.reference,
            "execution_envelope_revision": 1,
            "execution_envelope_digest": digest(self.payload),
        }

    def test_admits_only_the_bound_owner_class_and_path_scope(self) -> None:
        allowed = preflight(
            self.root, self.meta(), "worker", "source-write", ["src/module.py"], self.worktree
        )
        self.assertTrue(allowed.allowed)
        self.assertEqual("ALLOWED", allowed.code)
        denied = preflight(
            self.root, self.meta(), "worker", "source-write", ["../outside"], self.worktree
        )
        self.assertFalse(denied.allowed)
        self.assertEqual("PATH_OUTSIDE_SCOPE", denied.code)
        missing = preflight(self.root, self.meta(), "worker", "source-write", [], self.worktree)
        self.assertFalse(missing.allowed)
        self.assertEqual("SCOPE_DECLARATION_REQUIRED", missing.code)

    def test_rejects_mutated_bytes_and_hostile_shape(self) -> None:
        bound_meta = self.meta()
        mutated = envelope()
        mutated["non_goals"] = ["changed"]
        self.write_envelope(mutated)
        result = preflight(
            self.root, bound_meta, "worker", "test", ["tests/test_x.py"], self.worktree
        )
        self.assertFalse(result.allowed)
        self.assertEqual("ENVELOPE_DIGEST_MISMATCH", result.code)
        hostile = envelope()
        hostile["unknown"] = True
        self.assertIn("envelope unknown field: unknown", errors(hostile))

    def test_never_infers_owner_or_authorization_from_envelope_text(self) -> None:
        result = preflight(self.root, self.meta(), "other-worker", "test", [], self.worktree)
        self.assertFalse(result.allowed)
        self.assertEqual("OWNER_CLAIM_REQUIRED", result.code)
        forbidden = preflight(
            self.root, self.meta(), "worker", "git-publication", [], self.worktree
        )
        self.assertFalse(forbidden.allowed)
        self.assertEqual("COMMAND_CLASS_DENIED", forbidden.code)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
