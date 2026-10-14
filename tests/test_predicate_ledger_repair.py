# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Contract tests for audited post-acceptance predicate-ledger repair."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.predicate_ledger_repair import repair


class PredicateLedgerRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "candidate.txt").write_text("candidate", encoding="utf-8")
        (self.root / "candidate-new.txt").write_text("candidate refreshed", encoding="utf-8")
        (self.root / "evidence.txt").write_text("fresh evidence", encoding="utf-8")
        (self.root / "spec.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "spec_ref": "spec.json",
                    "spec_revision": 1,
                    "acceptance_predicates": [{"id": "predicate", "description": "pass"}],
                    "definition_of_done": ["pass"],
                    "inputs": [{"id": "input", "description": "input"}],
                    "outputs": [{"id": "output", "description": "output"}],
                    "allowed_tools": ["source.read"],
                    "forbidden_tools": [],
                    "required_evidence_classes": ["contract-test"],
                    "gates": [{"id": "gate", "description": "pass"}],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def digest(self, name: str) -> str:
        return "sha256:" + hashlib.sha256((self.root / name).read_bytes()).hexdigest()

    def args(self, **changes: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "expected_revision": 4,
            "predicate": "predicate",
            "candidate_ref": "candidate-new.txt",
            "candidate_digest": self.digest("candidate-new.txt"),
            "evidence_class": "contract-test",
            "evidence_ref": "evidence.txt",
            "evidence_digest": self.digest("evidence.txt"),
            "reason": "audited fresh evidence after acceptance",
        }
        values.update(changes)
        return argparse.Namespace(**values)

    def meta(self) -> dict[str, object]:
        return {
            "id": "AR-0125",
            "task_revision": 4,
            "status": "done",
            "owner": "",
            "claim_expires": "",
            "spec_ref": "spec.json",
            "spec_revision": 1,
            "evidence_ledger": {
                "candidate_ref": "candidate.txt",
                "candidate_digest": self.digest("candidate.txt"),
                "steps": [
                    {
                        "predicate_id": "predicate",
                        "status": "pass",
                        "candidate_ref": "candidate.txt",
                        "candidate_digest": self.digest("candidate.txt"),
                        "evidence_class": "contract-test",
                        "evidence_ref": "evidence.txt",
                        "evidence_digest": self.digest("evidence.txt"),
                        "recorded_task_revision": 3,
                    }
                ],
            },
        }

    def test_repairs_named_predicate_and_preserves_prior_row(self) -> None:
        meta = self.meta()
        repair(self.root, self.args(), meta, None)
        steps = meta["evidence_ledger"]["steps"]  # type: ignore[index]
        self.assertEqual(["stale", "pass"], [step["status"] for step in steps])

    def test_appends_when_retained_history_is_invalid(self) -> None:
        meta = self.meta()
        malformed = {"historical": "retained exactly"}
        meta["evidence_ledger"]["steps"].append(malformed)  # type: ignore[index]
        repair(self.root, self.args(), meta, None)
        steps = meta["evidence_ledger"]["steps"]  # type: ignore[index]
        self.assertEqual(malformed, steps[1])
        self.assertEqual("pass", steps[2]["status"])

    def test_rejects_same_candidate_even_with_fresh_evidence(self) -> None:
        meta = self.meta()
        with self.assertRaisesRegex(RuntimeError, "not new"):
            repair(
                self.root,
                self.args(
                    candidate_ref="candidate.txt",
                    candidate_digest=self.digest("candidate.txt"),
                ),
                meta,
                None,
            )

    def test_rejects_active_owned_stale_and_traversal_inputs(self) -> None:
        for field, value, message in (
            ("status", "in_progress", "inactive"),
            ("owner", "worker", "inactive"),
        ):
            meta = self.meta()
            meta[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, message):
                repair(self.root, self.args(), meta, None)
        with self.assertRaisesRegex(RuntimeError, "stale revision"):
            repair(self.root, self.args(expected_revision=3), self.meta(), None)
        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            repair(self.root, self.args(evidence_ref="../evidence.txt"), self.meta(), None)

    def test_rejects_malformed_ledger_and_unbounded_reason(self) -> None:
        meta = self.meta()
        meta["evidence_ledger"] = {"steps": []}
        with self.assertRaisesRegex(RuntimeError, "canonical"):
            repair(self.root, self.args(), meta, None)
        with self.assertRaisesRegex(RuntimeError, "unbounded"):
            repair(self.root, self.args(reason="x" * 513), self.meta(), None)


if __name__ == "__main__":
    unittest.main()
