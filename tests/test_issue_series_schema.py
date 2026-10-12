# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Strict schema coverage for local issue-series records."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any, ClassVar

import jsonschema

ROOT = Path(__file__).resolve().parents[1]


def manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "repository": "github.com/example/coordinator",
        "issue": {"number": 42, "url": "https://github.com/example/coordinator/issues/42"},
        "issue_digest": "sha256:" + "a" * 64,
        "members": [
            {
                "id": "AR-1000",
                "short_title": "Create records",
                "scope": "Create local task records",
                "acceptance": "Focused tests pass",
                "ownership": "Coordinator core",
                "depends_on": [],
            }
        ],
        "finalizer": "AR-1000",
    }


def task_record() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": "AR-1000",
        "title": "Create issue series",
        "status": "open",
        "priority": "P1",
        "summary": "Create local records.",
        "next_action": "Run focused tests.",
        "task_revision": 1,
        "updated_at": "2026-10-12T00:00:00+00:00",
        "issue_series": {
            "manifest_ref": "issue-series/example-coordinator-42.json",
            "manifest_digest": "sha256:" + "b" * 64,
            "member_id": "AR-1000",
        },
    }


class IssueSeriesSchemaTests(unittest.TestCase):
    manifest_validator: ClassVar[Any]
    task_validator: ClassVar[Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest_validator = jsonschema.Draft202012Validator(
            json.loads((ROOT / "schema/issue-series-manifest.schema.json").read_text())
        )
        cls.task_validator = jsonschema.Draft202012Validator(
            json.loads((ROOT / "schema/task-record.schema.json").read_text())
        )

    def assert_manifest_invalid(self, value: object) -> None:
        with self.assertRaises(jsonschema.ValidationError):
            self.manifest_validator.validate(value)

    def assert_task_invalid(self, value: object) -> None:
        with self.assertRaises(jsonschema.ValidationError):
            self.task_validator.validate(value)

    def test_manifest_accepts_strict_local_shape(self) -> None:
        self.manifest_validator.validate(manifest())

    def test_manifest_rejects_private_or_unbounded_shape(self) -> None:
        cases: list[dict[str, Any]] = []
        value = manifest()
        value["issue"]["body"] = "private issue text"
        cases.append(value)
        value = manifest()
        value["repository"] = "github.com/Example/coordinator"
        cases.append(value)
        value = manifest()
        value["issue_digest"] = "sha256:" + "A" * 64
        cases.append(value)
        value = manifest()
        value["members"][0]["scope"] = "line one\nline two"
        cases.append(value)
        for value in cases:
            with self.subTest(value=value):
                self.assert_manifest_invalid(value)

    def test_task_record_accepts_complete_immutable_linkage(self) -> None:
        self.task_validator.validate(task_record())

    def test_task_record_rejects_partial_or_unsafe_linkage(self) -> None:
        cases: list[dict[str, Any]] = []
        value = task_record()
        del value["issue_series"]["member_id"]
        cases.append(value)
        value = task_record()
        value["issue_series"]["manifest_ref"] = "../issue-series/example.json"
        cases.append(value)
        value = task_record()
        value["issue_series"]["manifest_digest"] = "sha256:" + "A" * 64
        cases.append(value)
        value = task_record()
        value["issue_series"]["extra"] = "not allowed"
        cases.append(value)
        for value in cases:
            with self.subTest(value=value):
                self.assert_task_invalid(value)

    def test_linkage_is_independent_per_record(self) -> None:
        first = task_record()
        second = copy.deepcopy(first)
        second["id"] = "AR-1001"
        second["issue_series"]["member_id"] = "AR-1001"
        self.task_validator.validate(first)
        self.task_validator.validate(second)


if __name__ == "__main__":
    unittest.main()
