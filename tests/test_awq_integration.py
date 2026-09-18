"""Regression tests for the pinned AWQ trust/profile integration."""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


class AwqIntegrationTests(unittest.TestCase):
    def test_lock_retains_formal_profile_and_reviewed_registry(self) -> None:
        lock = json.loads((ROOT / "quality/awq.lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["awq_version"], "0.35.0")
        self.assertEqual(
            lock["registry_sha256"],
            "81f02acd265c77443175e8e353d5c5793dddb9fad6e45f5dea2402d2968ea11e",
        )
        self.assertIn("formal-evidence", lock["profiles"])

    def test_trust_policy_has_disjoint_event_classes_and_existing_workflows(self) -> None:
        policy = json.loads((ROOT / "quality/workflow-trust.json").read_text(encoding="utf-8"))
        events = policy["events"]
        classes = [set(values) for values in events.values()]
        self.assertEqual(sum(map(len, classes)), len(set().union(*classes)))
        for workflow in (*policy["required_gate_workflows"], *policy["publication_workflows"]):
            self.assertTrue((ROOT / workflow).is_file(), workflow)
        self.assertEqual(events["prohibited"], ["pull_request_target"])

    def test_trust_policy_rejects_overlapping_event_class_as_hostile_fixture(self) -> None:
        policy = json.loads((ROOT / "quality/workflow-trust.json").read_text(encoding="utf-8"))
        policy["events"]["prohibited"].append("push")
        classes = [set(values) for values in policy["events"].values()]
        self.assertNotEqual(sum(map(len, classes)), len(set().union(*classes)))
