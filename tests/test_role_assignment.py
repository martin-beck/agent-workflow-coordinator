# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Positive and hostile tests for role assignments."""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tools import role_assignment
from tools.role_assignment import (
    RoleAssignmentError,
    assignment_errors,
    check_assignment,
    validate_assignment,
)
from tools.role_registry import RoleRegistryError, check_registry, registry_errors

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples/roles"


def load_fixture(name: str) -> dict[str, Any]:
    value = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("fixture must be an object")
    return value


class RoleAssignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = load_fixture("role-registry.json")

    def test_one_owner_may_hold_multiple_roles_with_independent_expiry(self) -> None:
        assignment = validate_assignment(
            load_fixture("role-assignment.json"),
            self.registry,
            now=dt.datetime(2026, 9, 24, tzinfo=dt.UTC),
        )
        self.assertEqual(
            ["implementer", "reviewer"], [item["role_id"] for item in assignment["roles"]]
        )

    def test_unknown_role_and_expiry_fail_closed(self) -> None:
        for fixture, message in (
            ("hostile-unknown-role-assignment.json", "unknown role"),
            ("hostile-expired-role-assignment.json", "expired role assignment"),
            ("hostile-secret-assignment.json", "schema:"),
        ):
            with (
                self.subTest(fixture=fixture),
                self.assertRaisesRegex(RoleAssignmentError, message),
            ):
                validate_assignment(
                    load_fixture(fixture),
                    self.registry,
                    now=dt.datetime(2026, 9, 24, tzinfo=dt.UTC),
                )

    def test_duplicate_role_and_naive_validation_time_fail_closed(self) -> None:
        assignment = load_fixture("role-assignment.json")
        assignment["roles"].append(dict(assignment["roles"][0]))
        with self.assertRaisesRegex(RoleAssignmentError, "more than once"):
            validate_assignment(
                assignment, self.registry, now=dt.datetime(2026, 9, 24, tzinfo=dt.UTC)
            )
        with self.assertRaisesRegex(RoleAssignmentError, "timezone-aware"):
            validate_assignment(
                load_fixture("role-assignment.json"),
                self.registry,
                now=dt.datetime.fromisoformat("2026-09-24T00:00:00"),
            )

    def test_validation_rejects_non_objects_and_unavailable_registry(self) -> None:
        self.assertEqual(
            ["role assignment must be an object"], assignment_errors(None, self.registry)
        )
        self.assertEqual(
            ["registry: role registry must be an object"],
            assignment_errors(load_fixture("role-assignment.json"), None),
        )
        self.assertEqual(
            ["registry: roles are unavailable"],
            assignment_errors(load_fixture("role-assignment.json"), {}),
        )
        self.assertEqual(["role registry must be an object"], registry_errors(None))

    def test_file_checkers_and_read_errors_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / "registry.json"
            assignment = root / "assignment.json"
            registry.write_text(json.dumps(self.registry), encoding="utf-8")
            assignment.write_text(
                (FIXTURES / "role-assignment.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            check_registry(registry)
            check_assignment(assignment, registry, now=dt.datetime(2026, 9, 24, tzinfo=dt.UTC))
            with self.assertRaises(RoleRegistryError):
                check_registry(root / "missing.json")
            bad = root / "bad.json"
            bad.write_text("[]", encoding="utf-8")
            with self.assertRaises(RoleRegistryError):
                check_registry(bad)
            with self.assertRaises(RoleAssignmentError):
                check_assignment(assignment, root / "missing.json")

    def test_dependency_free_validator_rejects_schema_boundaries(self) -> None:
        """The vendored assignment validator has no hidden package dependency."""
        self.assertTrue(role_assignment._assignment_role_errors(None))
        self.assertTrue(role_assignment._assignment_role_errors([{}]))
        self.assertTrue(
            role_assignment._assignment_role_errors([{"role_id": "x", "expires_at": "not-a-date"}])
        )
        for invalid_timestamp in ("2099-01-01 00:00:00+00:00", "2099-01-01T00:00:00+00:00:01"):
            with self.subTest(invalid_timestamp=invalid_timestamp):
                self.assertTrue(
                    role_assignment._assignment_role_errors(
                        [{"role_id": "implementer", "expires_at": invalid_timestamp}]
                    )
                )
        self.assertTrue(role_assignment._evidence_errors(None))
        self.assertTrue(role_assignment._evidence_errors([{}]))
        self.assertTrue(
            role_assignment._evidence_errors(
                [{"kind": "invalid", "ref": "bad ref", "digest": "bad"}]
            )
        )
        self.assertTrue(
            role_assignment._evidence_errors(
                [{"kind": [], "ref": "AR-0001", "digest": "sha256:" + "a" * 64}]
            )
        )
        assignment = load_fixture("role-assignment.json")
        assignment["schema_version"] = True
        self.assertTrue(assignment_errors(assignment, self.registry))
        assignment = load_fixture("role-assignment.json")
        assignment["roles"][0]["expires_at"] = "2099-01-01t00:00:00z"
        self.assertEqual([], assignment_errors(assignment, self.registry))
        assignment = load_fixture("role-assignment.json")
        assignment.update(schema_version=2, assignment_id="x", owner_id="x")
        assignment["roles"] = []
        assignment["authorization_evidence"] = []
        self.assertTrue(assignment_errors(assignment, self.registry))


if __name__ == "__main__":
    unittest.main()
