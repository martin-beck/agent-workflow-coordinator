# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Tests for the structured bounded-model evidence contract."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import re
import runpy
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FORMAL_ROOT = ROOT / "formal" / "handoffctl"
SET_BOUND_NAMES = {
    "Actors": "max_actors",
    "Processes": "max_processes",
    "Projects": "max_projects",
    "Tasks": "max_tasks",
    "Worktrees": "max_worktrees",
}


def load_evidence() -> dict[str, Any]:
    value = json.loads((ROOT / "formal" / "evidence.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("formal evidence must be a JSON object")
    return value


def configured_bounds(configs: list[Path]) -> dict[str, int]:
    bounds = dict.fromkeys(SET_BOUND_NAMES.values(), 0)
    bounds["max_revision"] = 0
    for config in configs:
        text = config.read_text(encoding="utf-8")
        for constant, name in SET_BOUND_NAMES.items():
            match = re.search(rf"^\s*{constant}\s*=\s*\{{([^}}]+)\}}", text, re.MULTILINE)
            if match is not None:
                size = len([item for item in match.group(1).split(",") if item.strip()])
                bounds[name] = max(bounds[name], size)
        revision = re.search(r"^\s*MaxRevision\s*=\s*(\d+)", text, re.MULTILINE)
        if revision is not None:
            bounds["max_revision"] = max(bounds["max_revision"], int(revision.group(1)))
    bounds["model_count"] = len(configs)
    return {name: value for name, value in bounds.items() if value > 0}


class FormalEvidenceTests(unittest.TestCase):
    def test_sqlite_correspondence_nonclaims_are_nonempty(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        nonclaims = artifact["evidence"]["nonclaims"]
        self.assertIsInstance(nonclaims, list)
        self.assertTrue(nonclaims)
        self.assertEqual(len(nonclaims), len(set(nonclaims)))
        self.assertTrue(
            all(isinstance(nonclaim, str) and nonclaim.strip() for nonclaim in nonclaims)
        )

    def test_sqlite_correspondence_claim_stays_unproven(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        self.assertEqual(artifact["evidence"]["correspondence_claim"], "not-proven")

    def test_sqlite_correspondence_evidence_stays_bounded(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        self.assertEqual(artifact["evidence"]["status"], "bounded-trace-map-only")

    def test_sqlite_correspondence_mutation_gate_is_rejection_only(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        gate = artifact["implementation"]["mutation_gate"]
        self.assertIsInstance(gate, str)
        self.assertTrue(gate)
        self.assertEqual(gate, "rejection-only")

    def test_sqlite_correspondence_revision_shape(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        revision = artifact["implementation"]["revision"]
        self.assertIsInstance(revision, str)
        self.assertRegex(revision, r"^[0-9a-f]{40}$")

    def test_sqlite_correspondence_source_digest_shapes(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        for digest in (
            artifact["model"]["sha256"],
            artifact["model"]["config_sha256"],
        ):
            self.assertIsInstance(digest, str)
            self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_sqlite_correspondence_module_digest_shape(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        self.assertIsInstance(artifact["implementation"], dict)
        digest = artifact["implementation"]["module_sha256"]
        self.assertIsInstance(digest, str)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_sqlite_correspondence_schema_version_is_positive_integer(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        version = artifact["schema_version"]
        self.assertIs(type(version), int)
        self.assertGreater(version, 0)

    def test_sqlite_correspondence_model_paths_are_safe_files(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        self.assertIsInstance(artifact["model"], dict)
        for relative in (artifact["model"]["path"], artifact["model"]["config"]):
            self.assertIsInstance(relative, str)
            self.assertTrue(relative)
            path = Path(relative)
            self.assertTrue(relative)
            self.assertFalse(path.is_absolute())
            self.assertNotIn("..", path.parts)
            expected_suffix = ".cfg" if relative == artifact["model"]["config"] else ".tla"
            self.assertEqual(path.suffix, expected_suffix)
            self.assertTrue((ROOT / path).is_file())
            self.assertFalse((ROOT / path).is_symlink())

    def test_sqlite_correspondence_module_path_is_safe(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        module = artifact["implementation"]["module"]
        self.assertIsInstance(module, str)
        self.assertTrue(module)
        self.assertTrue(module)
        module_path = Path(module)
        self.assertFalse(module_path.is_absolute())
        self.assertNotIn("..", module_path.parts)
        self.assertTrue((ROOT / module_path).is_file())
        self.assertFalse((ROOT / module_path).is_symlink())

    def test_sqlite_transition_model_mappings_have_actions(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        for transition in artifact["transitions"]:
            model = transition["model"]
            self.assertIsInstance(model, dict, transition["name"])
            self.assertIsInstance(model.get("action"), str, transition["name"])

    def test_sqlite_transition_implementation_mappings_are_qualified(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        for transition in artifact["transitions"]:
            implementations = transition["implementation"]
            self.assertIsInstance(implementations, list, transition["name"])
            self.assertTrue(implementations, transition["name"])
            for qualified_path in implementations:
                self.assertIsInstance(qualified_path, str, transition["name"])
                parts = qualified_path.split(".")
                self.assertTrue(all(part.strip() for part in parts), transition["name"])
                self.assertTrue(
                    len(parts) >= 2 or qualified_path.startswith("_"),
                    transition["name"],
                )

    def test_sqlite_transition_implementation_mappings_are_unique(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        for transition in artifact["transitions"]:
            implementations = transition["implementation"]
            self.assertEqual(len(implementations), len(set(implementations)), transition["name"])

    def test_sqlite_transition_names_are_unique(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        transitions = artifact["transitions"]
        self.assertIsInstance(transitions, list)
        self.assertTrue(transitions)
        self.assertTrue(all(isinstance(transition, dict) for transition in transitions))
        names = [transition["name"] for transition in transitions]
        self.assertTrue(all(isinstance(name, str) and name.strip() for name in names))
        self.assertTrue(names)
        self.assertEqual(len(names), len(set(names)))

    def test_sqlite_transition_postconditions_are_nonempty(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        for transition in artifact["transitions"]:
            postcondition = transition["postcondition"]
            self.assertIsInstance(postcondition, str)
            self.assertTrue(postcondition.strip(), transition["name"])
            if transition["model"]["action"] == "Crash":
                self.assertIn("write-closed", postcondition)
                self.assertIn("permanently", postcondition)
                self.assertIn("safe_mode", postcondition)
                self.assertIn("ambiguous", postcondition)
            else:
                self.assertTrue(
                    "state-preserving" in postcondition or "without changing" in postcondition,
                    transition["name"],
                )
                self.assertNotIn("write-closed", postcondition)
                self.assertNotIn("permanently", postcondition)

    def test_sqlite_model_actions_are_in_next(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        model = (ROOT / artifact["model"]["path"]).read_text(encoding="utf-8")
        next_definition = re.search(
            r"^Next\s*==(?P<body>.*?)(?=^FunctionalAvailability)",
            model,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(next_definition)
        assert next_definition is not None
        for transition in artifact["transitions"]:
            action = transition["model"]["action"]
            if action != "no-op":
                self.assertIn(f"{action}(", next_definition.group("body"), action)

    def test_upgrade_next_declared_before_spec(self) -> None:
        model = (ROOT / "formal" / "upgrade" / "UpgradeRecovery.tla").read_text()
        next_match = re.search(r"^Next\s*==", model, re.MULTILINE)
        spec_match = re.search(r"^Spec\s*==", model, re.MULTILINE)
        self.assertIsNotNone(next_match)
        self.assertIsNotNone(spec_match)
        assert next_match is not None
        assert spec_match is not None
        self.assertLess(next_match.start(), spec_match.start())

    def test_upgrade_vars_declared_before_spec(self) -> None:
        model = (ROOT / "formal" / "upgrade" / "UpgradeRecovery.tla").read_text()
        vars_match = re.search(r"^vars\s*==", model, re.MULTILINE)
        spec_match = re.search(r"^Spec\s*==", model, re.MULTILINE)
        self.assertIsNotNone(vars_match)
        self.assertIsNotNone(spec_match)
        assert vars_match is not None
        assert spec_match is not None
        self.assertLess(vars_match.start(), spec_match.start())

    def test_upgrade_model_vars_tuple_matches_variables(self) -> None:
        model = (ROOT / "formal" / "upgrade" / "UpgradeRecovery.tla").read_text()
        declaration = re.search(r"^VARIABLES\s+(.+)$", model, re.MULTILINE)
        tuple_match = re.search(r"^vars\s*==\s*<<(.+)>>$", model, re.MULTILINE)
        self.assertIsNotNone(declaration)
        self.assertIsNotNone(tuple_match)
        assert declaration is not None
        assert tuple_match is not None
        variables = [name.strip() for name in declaration.group(1).split(",")]
        state_tuple = [name.strip() for name in tuple_match.group(1).split(",")]
        self.assertEqual(state_tuple, variables)

    def test_upgrade_model_variables_are_unique(self) -> None:
        model = (ROOT / "formal" / "upgrade" / "UpgradeRecovery.tla").read_text()
        declaration = re.search(r"^VARIABLES\s+(.+)$", model, re.MULTILINE)
        self.assertIsNotNone(declaration)
        assert declaration is not None
        variables = [name.strip() for name in declaration.group(1).split(",")]
        self.assertTrue(variables)
        self.assertEqual(len(variables), len(set(variables)))

    def test_upgrade_config_invariants_are_theorems(self) -> None:
        config = (ROOT / "formal" / "upgrade" / "UpgradeRecovery.cfg").read_text()
        model = (ROOT / "formal" / "upgrade" / "UpgradeRecovery.tla").read_text()
        invariants = re.findall(r"^INVARIANT\s+(\w+)$", config, re.MULTILINE)
        self.assertTrue(invariants)
        self.assertEqual(len(invariants), len(set(invariants)))
        spec_start = model.index("Spec ==")
        for invariant in invariants:
            theorem = re.search(rf"^THEOREM\s+Spec\s+=>\s+\[\]{invariant}$", model, re.MULTILINE)
            self.assertIsNotNone(theorem, invariant)
            assert theorem is not None
            self.assertGreater(theorem.start(), spec_start, invariant)

    def test_upgrade_spec_composes_init_and_next(self) -> None:
        model = (ROOT / "formal" / "upgrade" / "UpgradeRecovery.tla").read_text()
        specification = re.search(
            r"^Spec\s*==(?P<body>.*?)(?=^THEOREM)", model, re.MULTILINE | re.DOTALL
        )
        self.assertIsNotNone(specification)
        assert specification is not None
        self.assertRegex(specification.group("body"), r"\bInit\b")
        self.assertRegex(specification.group("body"), r"\[\]\[Next\]_vars")
        self.assertEqual(specification.group("body").count("[][Next]_vars"), 1)

    def test_upgrade_config_constants_exist_in_model(self) -> None:
        config = (ROOT / "formal" / "upgrade" / "UpgradeRecovery.cfg").read_text()
        model = (ROOT / "formal" / "upgrade" / "UpgradeRecovery.tla").read_text()
        constants = re.findall(r"^CONSTANT\s+(\w+)\s*=", config, re.MULTILINE)
        self.assertTrue(constants)
        self.assertEqual(len(constants), len(set(constants)))
        declaration = re.search(r"^CONSTANTS\s+(.+)$", model, re.MULTILINE)
        self.assertIsNotNone(declaration)
        assert declaration is not None
        declared = {name.strip() for name in declaration.group(1).split(",")}
        self.assertEqual(len(declaration.group(1).split(",")), len(declared))
        self.assertTrue(set(constants) <= declared)

    def test_upgrade_config_specification_exists_in_model(self) -> None:
        config = (ROOT / "formal" / "upgrade" / "UpgradeRecovery.cfg").read_text()
        model = (ROOT / "formal" / "upgrade" / "UpgradeRecovery.tla").read_text()
        specification = re.search(r"^SPECIFICATION\s+(\w+)$", config, re.MULTILINE)
        self.assertIsNotNone(specification)
        assert specification is not None
        self.assertEqual(len(re.findall(r"^SPECIFICATION\s+\w+$", config, re.MULTILINE)), 1)
        self.assertIsNotNone(
            re.search(
                rf"^\s*{re.escape(specification.group(1))}\s*==",
                model,
                re.MULTILINE,
            ),
            specification.group(1),
        )

    def test_upgrade_config_invariants_exist_in_model(self) -> None:
        config = (ROOT / "formal" / "upgrade" / "UpgradeRecovery.cfg").read_text()
        model = (ROOT / "formal" / "upgrade" / "UpgradeRecovery.tla").read_text()
        invariants = re.findall(r"^INVARIANT\s+(\w+)$", config, re.MULTILINE)
        self.assertTrue(invariants)
        for invariant in invariants:
            self.assertIsNotNone(
                re.search(rf"^\s*{re.escape(invariant)}\s*==", model, re.MULTILINE),
                invariant,
            )

    def test_sqlite_correspondence_revision_binds_module_digest(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        revision = artifact["implementation"]["revision"]
        module = artifact["implementation"]["module"]
        recorded = subprocess.check_output(  # noqa: S603 - fixed Git provenance query
            ["git", "show", f"{revision}:{module}"],  # noqa: S607 - fixed Git query
            cwd=ROOT,
        )
        self.assertEqual(
            artifact["implementation"]["module_sha256"],
            hashlib.sha256(recorded).hexdigest(),
        )

    def test_sqlite_correspondence_revision_binds_config_digest(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        revision = artifact["implementation"]["revision"]
        config = artifact["model"]["config"]
        recorded = subprocess.check_output(  # noqa: S603 - fixed Git provenance query
            ["git", "show", f"{revision}:{config}"],  # noqa: S607 - fixed Git query
            cwd=ROOT,
        )
        self.assertEqual(
            artifact["model"]["config_sha256"],
            hashlib.sha256(recorded).hexdigest(),
        )

    def test_sqlite_correspondence_referenced_paths_exist(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        for field in ("path", "config"):
            self.assertTrue((ROOT / artifact["model"][field]).is_file())
        self.assertTrue((ROOT / artifact["implementation"]["module"]).is_file())

    def test_sqlite_correspondence_provenance_is_bounded(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        self.assertRegex(artifact["implementation"]["revision"], r"^[0-9a-f]{40}$")
        status = artifact["evidence"]["status"]
        self.assertIsInstance(status, str)
        self.assertTrue(status)
        self.assertEqual("bounded-trace-map-only", status)
        claim = artifact["evidence"]["correspondence_claim"]
        self.assertIsInstance(claim, str)
        self.assertTrue(claim)
        self.assertEqual("not-proven", claim)
        self.assertEqual("rejection-only", artifact["implementation"]["mutation_gate"])

    def test_sqlite_correspondence_nonclaims_cover_limits(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        nonclaims = artifact["evidence"]["nonclaims"]
        self.assertTrue(nonclaims)
        self.assertEqual(len(nonclaims), len(set(nonclaims)))
        joined = " ".join(nonclaims).lower()
        self.assertIn("refinement", joined)
        self.assertIn("authorize", joined)
        self.assertIn("crash", joined)
        self.assertIn("mutation", joined)
        self.assertIn("rollback", joined)
        self.assertIn("git dispatch", joined)
        self.assertIn("outcome publication", joined)
        self.assertIn("apply", joined)

    def test_sqlite_evidence_indexes_each_transition_once(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        transition_names = {transition["name"] for transition in artifact["transitions"]}
        indexed = artifact["evidence"]["by_transition"]
        self.assertEqual(transition_names, set(indexed))
        for name, tests in indexed.items():
            self.assertIsInstance(tests, list, name)
            self.assertTrue(tests, name)
            self.assertTrue(all(isinstance(test, str) and test.strip() for test in tests), name)

    def test_sqlite_evidence_references_have_test_shape(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        references = [
            reference
            for tests in artifact["evidence"]["by_transition"].values()
            for reference in tests
        ]
        pattern = re.compile(
            r"^tests/[A-Za-z0-9_/]+\.py::[A-Za-z_][A-Za-z0-9_]*\.test_[A-Za-z0-9_]+$"
        )
        self.assertTrue(references)
        self.assertTrue(all(pattern.fullmatch(reference) for reference in references))

    def test_sqlite_evidence_references_target_repository_tests(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        references = [
            reference
            for tests in artifact["evidence"]["by_transition"].values()
            for reference in tests
        ]
        for reference in references:
            path, selector = reference.split("::", 1)
            class_name, method_name = selector.split(".", 1)
            self.assertTrue((ROOT / path).is_file(), reference)
            self.assertTrue(class_name and method_name, reference)

    def test_sqlite_evidence_references_resolve_to_definitions(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        references = [
            reference
            for tests in artifact["evidence"]["by_transition"].values()
            for reference in tests
        ]
        for reference in references:
            path, selector = reference.split("::", 1)
            class_name, method_name = selector.split(".", 1)
            tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
            classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
            target = next(node for node in classes if node.name == class_name)
            methods = [
                node.name
                for node in target.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            self.assertIn(method_name, methods, reference)
            self.assertTrue(method_name.startswith("test_"), reference)

    def test_sqlite_evidence_references_are_unique_per_transition(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        for name, references in artifact["evidence"]["by_transition"].items():
            self.assertEqual(len(references), len(set(references)), name)

    def test_sqlite_correspondence_paths_are_safe_repository_paths(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        for field in ("path", "config"):
            value = artifact["model"][field]
            path = Path(value)
            self.assertFalse(path.is_absolute())
            self.assertNotIn("..", path.parts)
            self.assertTrue(value.startswith("formal/upgrade/"))

    def test_sqlite_correspondence_names_and_evidence_are_unique(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        transitions = artifact["transitions"]
        names = [transition["name"] for transition in transitions]
        references = artifact["evidence"]["tests"]
        self.assertIsInstance(references, list)
        self.assertTrue(references)
        self.assertTrue(
            all(isinstance(reference, str) and reference.strip() for reference in references)
        )
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(references), len(set(references)))
        self.assertEqual(
            {"snapshot-read", "identity-reread", "read-or-close-uncertainty", "ambiguous-fence"},
            set(names),
        )
        self.assertTrue(all("::" in reference for reference in references))

    def test_sqlite_correspondence_covers_every_transition(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        names = {transition["name"] for transition in artifact["transitions"]}
        coverage = artifact["evidence"]["by_transition"]
        self.assertIsInstance(coverage, dict)
        self.assertEqual(names, set(coverage))
        for name in names:
            references = coverage[name]
            self.assertIsInstance(references, list, name)
            self.assertTrue(references, name)
            self.assertTrue(
                all(isinstance(reference, str) and reference.strip() for reference in references),
                name,
            )
        covered = {reference for values in coverage.values() for reference in values}
        self.assertEqual(covered, set(artifact["evidence"]["tests"]))
        for name, references in coverage.items():
            self.assertEqual(len(references), len(set(references)), name)

    def test_sqlite_correspondence_artifact_schema_is_explicit(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        self.assertIsInstance(artifact, dict)
        self.assertIsInstance(artifact["evidence"], dict)
        self.assertEqual(
            {"path", "sha256", "config", "config_sha256", "states", "barriers"},
            set(artifact["model"]),
        )
        self.assertEqual(
            {"revision", "module", "module_sha256", "mutation_gate"},
            set(artifact["implementation"]),
        )
        self.assertEqual(
            {"tests", "by_transition", "status", "correspondence_claim", "nonclaims"},
            set(artifact["evidence"]),
        )
        self.assertIsInstance(artifact["kind"], str)
        self.assertTrue(artifact["kind"])
        self.assertEqual(
            {"schema_version", "kind", "model", "implementation", "transitions", "evidence"},
            set(artifact),
        )
        self.assertEqual(1, artifact["schema_version"])
        self.assertEqual("rejection-only", artifact["implementation"]["mutation_gate"])
        self.assertEqual("bounded-trace-map-only", artifact["evidence"]["status"])
        self.assertEqual("not-proven", artifact["evidence"]["correspondence_claim"])
        for transition in artifact["transitions"]:
            self.assertEqual(
                {"name", "implementation", "model", "postcondition"},
                set(transition),
            )
            self.assertIsInstance(transition["implementation"], list)
            self.assertTrue(transition["implementation"])
            self.assertIn("action", transition["model"])

    def test_sqlite_correspondence_declarations_match_model_and_config(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        model = (ROOT / artifact["model"]["path"]).read_text(encoding="utf-8")
        config = (ROOT / artifact["model"]["config"]).read_text(encoding="utf-8")
        for field in ("states", "barriers"):
            values = artifact["model"][field]
            self.assertIsInstance(values, list, field)
            self.assertTrue(values, field)
            self.assertTrue(
                all(isinstance(value, str) and value.strip() for value in values),
                field,
            )
            self.assertEqual(len(values), len(set(values)), field)
        self.assertTrue(set(artifact["model"]["states"]).isdisjoint(artifact["model"]["barriers"]))
        self.assertRegex(model, r"Journals == .*\"running\".*\"safe_mode\"")
        self.assertRegex(model, r"Barriers == .*\"held\".*\"ambiguous\"")
        self.assertIn('CONSTANT Backends = {"git", "sqlite"}', config)
        self.assertIn("running", artifact["model"]["states"])
        self.assertIn("safe_mode", artifact["model"]["states"])
        self.assertIn("held", artifact["model"]["barriers"])
        self.assertIn("ambiguous", artifact["model"]["barriers"])
        actions = [transition["model"]["action"] for transition in artifact["transitions"]]
        self.assertEqual(1, actions.count("Crash"))
        self.assertGreaterEqual(actions.count("no-op"), 1)

    def test_sqlite_transition_domains_are_declared(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        states = set(artifact["model"]["states"])
        barriers = set(artifact["model"]["barriers"])
        allowed_actions = {"Crash", "no-op"}
        for transition in artifact["transitions"]:
            model = transition["model"]
            self.assertIn(model["action"], allowed_actions, transition["name"])
            for field, domain in (("journal_before", states), ("journal_after", states)):
                if field in model:
                    self.assertIn(model[field], domain, transition["name"])
            for field, domain in (("barrier_before", barriers), ("barrier_after", barriers)):
                if field in model:
                    self.assertIn(model[field], domain, transition["name"])

    def test_sqlite_identity_reread_rejection_is_explicit(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        states = set(artifact["model"]["states"])
        barriers = set(artifact["model"]["barriers"])
        reread = next(
            transition
            for transition in artifact["transitions"]
            if transition["name"] == "identity-reread"
        )
        self.assertEqual("reject", reread["model"]["failure_outcome"])
        for transition in artifact["transitions"]:
            for field, value in transition["model"].items():
                if field.startswith("journal"):
                    self.assertIn(value, states, f"{transition['name']}: {field}")
                if field.startswith("barrier"):
                    self.assertIn(value, barriers, f"{transition['name']}: {field}")
                if field in {"failure_outcome", "result"}:
                    self.assertEqual("reject", value, f"{transition['name']}: {field}")
            if transition["model"]["action"] == "Crash":
                self.assertEqual("safe_mode", transition["model"].get("journal_after"))
                self.assertEqual("ambiguous", transition["model"].get("barrier_after"))
                self.assertNotIn("result", transition["model"])
                self.assertNotIn("failure_outcome", transition["model"])
            else:
                self.assertNotIn("journal_after", transition["model"])
                self.assertNotIn("barrier_after", transition["model"])

    def test_sqlite_correspondence_model_digest_matches_exact_source(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        model_path = ROOT / artifact["model"]["path"]
        digest = hashlib.sha256(model_path.read_bytes()).hexdigest()
        self.assertEqual(artifact["model"]["sha256"], digest)

    def test_sqlite_correspondence_module_digest_matches_exact_source(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        module_path = ROOT / artifact["implementation"]["module"]
        digest = hashlib.sha256(module_path.read_bytes()).hexdigest()
        self.assertEqual(artifact["implementation"]["module_sha256"], digest)

    def test_sqlite_correspondence_config_digest_matches_exact_source(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        config_path = ROOT / artifact["model"]["config"]
        digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
        self.assertEqual(artifact["model"]["config_sha256"], digest)

    def test_sqlite_correspondence_implementation_symbols_resolve(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        source = (ROOT / artifact["implementation"]["module"]).read_text(encoding="utf-8")
        for transition in artifact["transitions"]:
            implementation = transition["implementation"]
            self.assertIsInstance(implementation, list, transition["name"])
            self.assertTrue(implementation, transition["name"])
            self.assertTrue(
                all(isinstance(symbol, str) and symbol.strip() for symbol in implementation),
                transition["name"],
            )
            self.assertEqual(len(implementation), len(set(implementation)), transition["name"])
            self.assertTrue(
                all(isinstance(symbol, str) and symbol.strip() for symbol in implementation),
                transition["name"],
            )
            for symbol in implementation:
                owner, separator, member = symbol.partition(".")
                qualified = owner and separator and re.fullmatch(r"\w+", member)
                local = not separator and re.fullmatch(r"\w+", symbol)
                self.assertTrue(qualified or local, symbol)
            for symbol in transition["implementation"]:
                name = symbol.rsplit(".", 1)[-1]
                self.assertIsNotNone(
                    re.search(rf"\bdef {re.escape(name)}\(", source),
                    f"{transition['name']}: {symbol}",
                )

    def test_sqlite_correspondence_model_actions_exist_or_are_state_preserving(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        model = (ROOT / artifact["model"]["path"]).read_text(encoding="utf-8")
        for transition in artifact["transitions"]:
            action = transition["model"]["action"]
            self.assertIsInstance(action, str)
            self.assertTrue(action.strip(), transition["name"])
            self.assertIn(action, {"no-op", "Crash"}, transition["name"])
            if action == "no-op":
                self.assertIn("state", transition["postcondition"])
            else:
                self.assertRegex(model, rf"\b{re.escape(action)}\(")

    def test_sqlite_correspondence_evidence_references_resolve_to_tests(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        for reference in artifact["evidence"]["tests"]:
            relative, qualified = reference.split("::", 1)
            _class_name, method_name = qualified.split(".", 1)
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIsNotNone(
                re.search(rf"^    def {re.escape(method_name)}\(", source, re.MULTILINE),
                reference,
            )

    def test_sqlite_correspondence_evidence_test_references_are_unique(self) -> None:
        artifact = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        references = artifact["evidence"]["tests"]
        self.assertEqual(len(references), len(set(references)))

    def test_sqlite_snapshot_correspondence_map_is_bounded_and_fail_closed(self) -> None:
        value = json.loads(
            (ROOT / "formal" / "upgrade" / "sqlite-snapshot-correspondence.json").read_text()
        )
        self.assertEqual("bounded-sqlite-snapshot-correspondence", value["kind"])
        self.assertEqual("formal/upgrade/UpgradeRecovery.tla", value["model"]["path"])
        self.assertEqual("rejection-only", value["implementation"]["mutation_gate"])
        transitions = {item["name"]: item for item in value["transitions"]}
        self.assertEqual(
            {"snapshot-read", "identity-reread", "read-or-close-uncertainty", "ambiguous-fence"},
            set(transitions),
        )
        uncertainty = transitions["read-or-close-uncertainty"]["model"]
        self.assertEqual("Crash", uncertainty["action"])
        self.assertEqual("safe_mode", uncertainty["journal_after"])
        self.assertEqual("ambiguous", uncertainty["barrier_after"])
        self.assertEqual("reject", transitions["ambiguous-fence"]["model"]["result"])
        self.assertEqual("bounded-trace-map-only", value["evidence"]["status"])
        self.assertEqual("not-proven", value["evidence"]["correspondence_claim"])
        self.assertTrue(value["evidence"]["nonclaims"])

    def test_evidence_has_exact_awq_v024_contract_fields(self) -> None:
        evidence = load_evidence()
        self.assertEqual(
            {
                "assumptions",
                "bounds",
                "correspondence",
                "evidence_class",
                "limitations",
                "non_claims",
                "schema_version",
                "scope",
            },
            set(evidence),
        )
        self.assertEqual(1, evidence["schema_version"])
        self.assertEqual("bounded-model", evidence["evidence_class"])
        self.assertEqual("handoffctl-coordination-models", evidence["scope"])
        self.assertEqual("not-proven", evidence["correspondence"])
        for field in ("assumptions", "limitations", "non_claims"):
            values = evidence[field]
            self.assertIsInstance(values, list)
            self.assertTrue(values)
            self.assertEqual(len(values), len(set(values)))

    def test_evidence_bounds_and_runner_match_tracked_models(self) -> None:
        tier_manifest = json.loads((ROOT / "formal" / "tier-evidence.json").read_text())
        full_models = tier_manifest["profiles"]["full-exhaustive"]["models"]
        configs = [FORMAL_ROOT / f"{model}.cfg" for model in full_models]
        models = {config.stem for config in FORMAL_ROOT.glob("*.cfg")}
        models.add("OracleInteractionGates")
        runner = (FORMAL_ROOT / "verify.sh").read_text(encoding="utf-8")
        invoked = set(re.findall(r"^\s*run_model\s+(\w+)(?:\s+\w+)?\s*$", runner, re.MULTILINE))

        self.assertEqual(models, invoked)
        bounds = load_evidence()["bounds"]
        model_bounds = configured_bounds(configs)
        self.assertEqual(model_bounds, {name: bounds[name] for name in model_bounds})
        for name in (
            "tlc_workers",
            "jvm_heap_mb",
            "hosted_portable_jvm_heap_mb",
            "memory_max_mb",
            "swap_max_mb",
            "cpu_quota_percent",
            "tasks_max",
            "runtime_max_seconds",
            "pr_runtime_max_seconds",
        ):
            self.assertIsInstance(bounds[name], int)
            self.assertGreater(bounds[name], 0)

    def test_formal_tiers_require_explicit_non_ambiguous_selection(self) -> None:
        verify = (FORMAL_ROOT / "verify.sh").read_text(encoding="utf-8")
        self.assertIn("--tier", verify)
        self.assertIn("portable-smoke", verify)
        self.assertIn("pr-fast", verify)
        self.assertIn("pr-publication", verify)
        self.assertIn("full-exhaustive", verify)
        manifest = json.loads((ROOT / "formal" / "tier-evidence.json").read_text())
        self.assertFalse(manifest["profiles"]["portable-smoke"]["exhaustive"])
        self.assertFalse(manifest["profiles"]["pr-fast"]["exhaustive"])
        self.assertEqual(
            ["HandoffctlFast", "OracleInteractionGates"],
            manifest["profiles"]["pr-fast"]["models"],
        )
        self.assertIn("safety-only", manifest["profiles"]["pr-fast"]["claims"])
        fast_config = (FORMAL_ROOT / "HandoffctlFast.cfg").read_text()
        self.assertIn("SPECIFICATION Spec", fast_config)
        self.assertNotIn("PROPERTIES", fast_config)
        self.assertNotIn("EventuallyBoundCallSucceeds", fast_config)
        fast_invocations = re.findall(
            r"^\s*run_model\s+(\w+)(?:\s+(\w+))?\s*$", verify, re.MULTILINE
        )
        self.assertIn(
            ("HandoffctlFast", ""), [(model, source or "") for model, source in fast_invocations]
        )
        self.assertIn("run_model OracleInteractionGates", verify)
        self.assertFalse(manifest["profiles"]["pr-publication"]["exhaustive"])
        self.assertTrue(manifest["profiles"]["full-exhaustive"]["exhaustive"])
        self.assertNotEqual(
            manifest["profiles"]["portable-smoke"]["models"],
            manifest["profiles"]["full-exhaustive"]["models"],
        )
        self.assertEqual(6, len(manifest["profiles"]["full-exhaustive"]["models"]))
        self.assertEqual(6, len(manifest["profiles"]["pr-publication"]["models"]))
        pr_config = (FORMAL_ROOT / "HandoffctlPR.cfg").read_text()
        self.assertIn("Processes = {p1}", pr_config)
        self.assertIn("EventualCompletion", pr_config)
        attest = (ROOT / "formal" / "handoffctl" / "attest.py").read_text(encoding="utf-8")
        self.assertIn("state_counts", attest)
        self.assertIn("state_counts are unavailable", attest)
        self.assertIn("of exhaustive exploration", attest)
        self.assertIn("attestation requires TLC_CGROUP_MODE=required", attest)
        self.assertIn("runner-produced outcome manifest", attest)

    def test_workflow_separates_fork_pr_publication_and_weekly_tiers(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "verify.yml").read_text()
        formal = (ROOT / ".github" / "workflows" / "formal.yml").read_text()
        self.assertIn(
            "github.event.pull_request.head.repo.full_name != github.repository", workflow
        )
        self.assertIn("TLC_TIER: portable-smoke", workflow)
        self.assertIn("TLC_CGROUP_MODE: portable", workflow)
        self.assertIn("release_sensitive", workflow)
        for release_path in (
            "pyproject.toml",
            "uv.lock",
            "CHANGELOG.md",
            "tools/vendor.py",
        ):
            self.assertIn(release_path, workflow)
        self.assertIn("tools/lifecycle_trace.py", workflow)
        self.assertIn("push:", formal)
        self.assertIn("workflow_dispatch:", formal)
        self.assertIn("schedule:", formal)
        self.assertIn("if: github.ref == 'refs/heads/main'", formal)
        self.assertIn("runs-on: [self-hosted, Linux, X64, agent-workflow-coordinator-ci]", formal)
        formal_step = formal[
            formal.index("      - name: Run required formal tier\n") : formal.index(
                "      - name: Publish exact-head tier attestation\n"
            )
        ]
        for resource_setting in (
            "TLC_CGROUP_MODE",
            "TLC_HEAP",
            "TLC_MEMORY_MAX",
            "TLC_SWAP_MAX",
            "TLC_TIMEOUT_SECONDS",
        ):
            self.assertIn(resource_setting, formal_step)
        self.assertIn("MemoryMax=6G", formal)
        self.assertIn("MemorySwapMax=6G", formal)
        self.assertIn("MemTotal", formal)
        self.assertIn("/proc/self/cgroup", formal)
        self.assertIn("/proc/self/mountinfo", formal)
        self.assertIn('cgroup_dir="${cgroup_mount%/}${cgroup_relative:-/}"', formal)
        self.assertIn('"${cgroup_dir}/memory.max"', formal)
        self.assertIn('"${cgroup_dir}/memory.swap.max"', formal)
        self.assertNotIn("needs.scope.outputs.release_sensitive", formal_step)
        self.assertIn("github.event_name == 'schedule' && 360", formal)
        timeout_expression = formal[
            formal.index("TLC_TIMEOUT_SECONDS:") : formal.index(
                "\n", formal.index("TLC_TIMEOUT_SECONDS:")
            )
        ]
        self.assertIn("'6000'", timeout_expression)
        self.assertIn("'1200'", timeout_expression)

    def test_attestation_rejects_failed_formal_outcomes(self) -> None:
        script = ROOT / "formal" / "handoffctl" / "attest.py"
        stderr = io.StringIO()
        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    str(script),
                    "--tier",
                    "portable-smoke",
                    "--status",
                    "oom",
                    "--output",
                    str(ROOT / "formal" / "_unused.json"),
                    "--jar",
                    str(script),
                    "--manifest",
                    str(script),
                    "--models",
                    "HandoffctlBinding",
                ],
            ),
            mock.patch.object(sys, "stderr", stderr),
            self.assertRaises(SystemExit),
        ):
            runpy.run_path(str(script), run_name="__main__")
        self.assertIn("failed or incomplete formal runs", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
