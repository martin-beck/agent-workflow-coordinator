# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Positive and hostile tests for the role registry contract."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tools import role_registry
from tools.role_registry import (
    RoleRegistryError,
    _load_json,
    check_registry,
    registry_errors,
    validate_registry,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples/roles"


def load_fixture(name: str) -> dict[str, Any]:
    value = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("fixture must be an object")
    return value


class RoleRegistryTests(unittest.TestCase):
    def test_positive_registry_fixture_is_valid(self) -> None:
        registry = validate_registry(load_fixture("role-registry.json"))
        self.assertEqual("implementer", registry["default_role"])

    def test_hostile_fixtures_fail_closed(self) -> None:
        for fixture, message in (
            ("hostile-unknown-field.json", "schema:"),
            ("hostile-malformed-capability.json", "schema:"),
            ("hostile-forbidden-alias.json", "forbidden role alias"),
        ):
            with self.subTest(fixture=fixture), self.assertRaisesRegex(RoleRegistryError, message):
                validate_registry(load_fixture(fixture))

    def test_unknown_and_duplicate_capability_actions_fail_closed(self) -> None:
        registry = load_fixture("role-registry.json")
        self.assertIsInstance(registry, dict)
        registry["roles"][0]["capabilities"][0]["actions"].append("unknown.action")
        registry["roles"][0]["forbidden_actions"].append("unknown.action")
        errors = registry_errors(registry)
        self.assertTrue(any("capability action is forbidden" in error for error in errors))

    def test_duplicate_role_and_capability_ids_are_rejected(self) -> None:
        registry = load_fixture("role-registry.json")
        self.assertIsInstance(registry, dict)
        registry["roles"].append(registry["roles"][0])
        errors = registry_errors(registry)
        self.assertIn("semantic: role_id values must be unique", errors)

    def test_semantic_conflicts_and_file_checker_fail_closed(self) -> None:
        registry = load_fixture("role-registry.json")
        registry["schema_version"] = True
        self.assertTrue(role_registry.registry_errors(registry))
        registry = load_fixture("role-registry.json")
        role = registry["roles"][0]
        role["schema_version"] = True
        self.assertTrue(role_registry._role_errors(role))
        role["schema_version"] = 1
        role["worktree_policy"]["path_patterns"] = ["line-one\nline-two"]
        self.assertEqual([], role_registry._role_errors(role))
        role["capabilities"].append(dict(role["capabilities"][0]))
        role["forbidden_actions"].append("source.read")
        role["tool_policy"]["allow"].append("authority.commit")
        role["tool_policy"]["deny"].append("authority.commit")
        errors = registry_errors(registry)
        self.assertTrue(any("duplicate capability_id" in error for error in errors))
        self.assertTrue(any("both allowed and forbidden" in error for error in errors))
        self.assertTrue(any("capability action is forbidden" in error for error in errors))
        registry["default_role"] = "missing"
        self.assertIn(
            "semantic: default_role must reference a declared role", registry_errors(registry)
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text(json.dumps(load_fixture("role-registry.json")), encoding="utf-8")
            check_registry(path)
            with self.assertRaises(RoleRegistryError):
                _load_json(Path(directory) / "missing.json")

    def test_dependency_free_validator_rejects_schema_boundaries(self) -> None:
        """The runtime validator covers every schema shape without site packages."""
        self.assertTrue(role_registry._exact_object(None, {"field"}, "item"))
        self.assertTrue(role_registry._exact_object({}, {"field"}, "item"))
        self.assertTrue(role_registry._exact_object({"field": 1, "extra": 2}, {"field"}, "item"))
        self.assertTrue(role_registry._string(1, "field", role_registry.ROLE_ID, 3, 64))
        self.assertTrue(role_registry._string("x", "field", role_registry.ROLE_ID, 3, 64))
        self.assertTrue(role_registry._string("INVALID", "field", role_registry.ROLE_ID, 3, 64))
        self.assertTrue(
            role_registry._unique_strings(
                ["same", "same"], "items", minimum=0, pattern=role_registry.ACTION
            )
        )
        self.assertTrue(
            role_registry._unique_strings([], "items", minimum=1, pattern=role_registry.ACTION)
        )
        self.assertTrue(
            role_registry._unique_strings([1], "items", minimum=0, pattern=role_registry.ACTION)
        )

        registry = load_fixture("role-registry.json")
        role = registry["roles"][0]
        capability = role["capabilities"][0]
        capability.update(capability_id="x", description="", actions=[], scope="invalid")
        self.assertTrue(role_registry._capability_errors(capability))
        capability["scope"] = []
        self.assertTrue(role_registry._capability_errors(capability))
        role.update(schema_version=2, role_id="x", title="", summary="", capabilities=[1])
        role["forbidden_actions"] = ["same", "same"]
        role["tool_policy"] = {"allow": [1]}
        role["worktree_policy"] = {"mode": "invalid", "path_patterns": []}
        role["awq_profile_refs"] = ["bad ref"]
        self.assertTrue(role_registry._role_errors(role))

        registry.update(schema_version=2, registry_id="x", roles=[1], default_role="x")
        registry["default_awq_profile_refs"] = []
        self.assertTrue(role_registry.registry_errors(registry))


if __name__ == "__main__":
    unittest.main()
