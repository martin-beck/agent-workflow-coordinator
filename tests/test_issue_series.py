# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Deterministic hostile and positive tests for pure issue-series validation."""

from __future__ import annotations

import copy
import unittest
from collections.abc import Callable
from typing import Any

from tools.issue_series import IssueSeriesValidationError, parse_manifest


def manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "repository": "github.com/example/coordinator",
        "issue": {"number": 42, "url": "https://github.com/example/coordinator/issues/42"},
        "issue_digest": "sha256:" + "a" * 64,
        "members": [
            {
                "id": "AR-1000",
                "short_title": "Parse manifest",
                "scope": "Parse a bounded manifest",
                "acceptance": "Focused validator tests pass",
                "ownership": "Coordinator core",
                "depends_on": [],
            },
            {
                "id": "AR-1001",
                "short_title": "Publish series",
                "scope": "Publish only after preflight",
                "acceptance": "Atomic publisher test passes",
                "ownership": "Coordinator core",
                "depends_on": [{"id": "AR-1000", "rationale": "Needs validated input"}],
            },
        ],
        "finalizer": "AR-1001",
    }


class IssueSeriesTests(unittest.TestCase):
    def assert_invalid(self, value: object, message: str) -> None:
        with self.assertRaisesRegex(IssueSeriesValidationError, message):
            parse_manifest(value)

    def test_valid_manifest_is_normalized_and_deterministic(self) -> None:
        value = manifest()
        first = parse_manifest(value)
        second = parse_manifest(copy.deepcopy(value))
        self.assertEqual(first, second)
        self.assertEqual("github.com/example/coordinator", first.repository)
        self.assertEqual(("AR-1000", "AR-1001"), tuple(member.id for member in first.members))

    def test_strict_version_and_object_shapes_fail_closed(self) -> None:
        self.assert_invalid([], "manifest must be an object")
        changes: list[tuple[Callable[[dict[str, Any]], None], str]] = [
            (self._wrong_version, "schema_version"),
            (self._unknown_field, "unknown fields"),
            (self._missing_finalizer, "missing fields"),
            (self._raw_issue_body, "unknown fields"),
        ]
        for change, message in changes:
            with self.subTest(message=message):
                value = manifest()
                change(value)
                self.assert_invalid(value, message)

    @staticmethod
    def _wrong_version(value: dict[str, Any]) -> None:
        value["schema_version"] = 2

    @staticmethod
    def _unknown_field(value: dict[str, Any]) -> None:
        value["extra"] = "no"

    @staticmethod
    def _missing_finalizer(value: dict[str, Any]) -> None:
        del value["finalizer"]

    @staticmethod
    def _raw_issue_body(value: dict[str, Any]) -> None:
        value["issue"]["body"] = "raw"

    def test_identity_digest_and_private_data_are_rejected(self) -> None:
        cases = [
            ("repository", "example/coordinator", "repository"),
            ("repository", "github.com/Example/coordinator", "lowercase"),
            ("issue_digest", "sha256:" + "A" * 64, "issue_digest"),
            ("issue_digest", "issue body retained", "issue_digest"),
            ("scope", "Read /home/alice/private", "private or prompt"),
            ("acceptance", "Use api key proof", "private or prompt"),
            ("ownership", "Prompt injection owner", "private or prompt"),
        ]
        for field, replacement, message in cases:
            with self.subTest(field=field):
                value = manifest()
                if field in {"scope", "acceptance", "ownership"}:
                    value["members"][0][field] = replacement
                else:
                    value[field] = replacement
                self.assert_invalid(value, message)
        value = manifest()
        value["issue"]["url"] = "https://github.com/other/project/issues/42"
        self.assert_invalid(value, "immutable issue number")

    def test_member_requirements_and_dependency_rationale_are_strict(self) -> None:
        cases = [
            ("short_title", "", "short_title"),
            ("short_title", "This title contains five words", "at most four words"),
            ("scope", "", "scope"),
            ("acceptance", "", "acceptance"),
            ("ownership", "", "ownership"),
        ]
        for field, replacement, message in cases:
            with self.subTest(field=field):
                value = manifest()
                value["members"][0][field] = replacement
                self.assert_invalid(value, message)
        value = manifest()
        value["members"][1]["depends_on"][0].pop("rationale")
        self.assert_invalid(value, "missing fields")

    def test_duplicate_cross_project_cycle_orphan_and_finalizer_fail(self) -> None:
        cases: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
            ("duplicate AR", self._duplicate_member),
            ("missing dependency", self._missing_dependency),
            ("dependency cycle", self._cycle),
            ("orphaned", self._orphan),
            ("ordered after", lambda value: value.update({"finalizer": "AR-1000"})),
        ]
        for message, change in cases:
            with self.subTest(message=message):
                value = manifest()
                change(value)
                self.assert_invalid(value, message)

    @staticmethod
    def _duplicate_member(value: dict[str, Any]) -> None:
        value["members"][1] = copy.deepcopy(value["members"][0])

    @staticmethod
    def _missing_dependency(value: dict[str, Any]) -> None:
        value["members"][1]["depends_on"][0] = {"id": "AR-9999", "rationale": "outside"}

    @staticmethod
    def _cycle(value: dict[str, Any]) -> None:
        value["members"][0]["depends_on"] = [{"id": "AR-1001", "rationale": "cycle"}]

    @staticmethod
    def _orphan(value: dict[str, Any]) -> None:
        value["members"][1]["depends_on"] = []

    def test_finalizer_must_be_member_and_edges_cannot_duplicate_or_self_reference(self) -> None:
        value = manifest()
        value["finalizer"] = "AR-9999"
        self.assert_invalid(value, "exactly one member")
        value = manifest()
        value["members"][1]["depends_on"].append({"id": "AR-1000", "rationale": "again"})
        self.assert_invalid(value, "duplicate dependencies")
        value = manifest()
        value["members"][0]["depends_on"] = [{"id": "AR-1000", "rationale": "self"}]
        self.assert_invalid(value, "depends on itself")

    def test_finalizer_directly_depends_on_every_non_finalizer(self) -> None:
        value = manifest()
        value["members"].append(
            {
                "id": "AR-1002",
                "short_title": "Finish locally",
                "scope": "Finish locally",
                "acceptance": "The final local check passes",
                "ownership": "Coordinator core",
                "depends_on": [{"id": "AR-1001", "rationale": "finalize"}],
            }
        )
        value["finalizer"] = "AR-1002"
        self.assert_invalid(value, "must directly depend on AR-1000")
