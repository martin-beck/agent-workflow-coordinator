# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Integrity checks for the bounded upgrade-model evidence snapshot."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast

from tools.update_upgrade_evidence import refresh

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "formal" / "upgrade" / "evidence.json"
CONTRACT = ROOT / "formal" / "upgrade" / "v10-refinement-contract.json"


class UpgradeFormalEvidenceTests(unittest.TestCase):
    def test_evidence_generator_refreshes_declared_file_digests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_path = root / "formal/upgrade/evidence.json"
            evidence_path.parent.mkdir(parents=True)
            tracked = root / "tracked.py"
            tracked.write_bytes(b"current\n")
            evidence_path.write_text(
                json.dumps({"implementation_snapshot": {"files": {"tracked.py": "old"}}}),
                encoding="utf-8",
            )
            refreshed = refresh(root)
            expected = hashlib.sha256(tracked.read_bytes()).hexdigest()
            snapshot = cast(dict[str, object], refreshed["implementation_snapshot"])
            files = cast(dict[str, str], snapshot["files"])
            self.assertEqual(expected, files["tracked.py"])
            self.assertEqual(
                expected,
                json.loads(evidence_path.read_text())["implementation_snapshot"]["files"][
                    "tracked.py"
                ],
            )

    def test_evidence_generator_rejects_missing_declared_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_path = root / "formal/upgrade/evidence.json"
            evidence_path.parent.mkdir(parents=True)
            evidence_path.write_text(
                json.dumps({"implementation_snapshot": {"files": {"missing.py": "old"}}}),
                encoding="utf-8",
            )
            with self.assertRaises(FileNotFoundError):
                refresh(root)

    def test_bound_rollback_inspection_evidence_maps_existing_tests_without_refinement_claim(
        self,
    ) -> None:
        evidence = json.loads(
            (ROOT / "formal" / "upgrade" / "rollback-inspection-evidence.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("implementation-test", evidence["evidence_class"])
        self.assertEqual("not-proven", evidence["implementation_refinement"])
        self.assertEqual(2, len(evidence["formal_obligations"]))
        revision = subprocess.run(  # noqa: S603
            ["git", "rev-parse", evidence["implementation_revision"]],  # noqa: S607
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        tree = subprocess.run(  # noqa: S603
            ["git", "rev-parse", f"{revision}^{{tree}}"],  # noqa: S607
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(tree, evidence["implementation_tree"])
        for tests in evidence["tests"].values():
            for reference in tests:
                path, selector = reference.split("::", 1)
                self.assertTrue((ROOT / path).exists(), path)
                # The file-level check is intentionally lightweight: unittest
                # selectors contain a class and method, while only the method
                # token is expected to occur literally in the source.
                self.assertIn(
                    selector.rsplit("::", 1)[-1],
                    (ROOT / path).read_text(encoding="utf-8"),
                    selector,
                )
        self.assertTrue(any("does not authorize" in claim for claim in evidence["claims"]))

    def test_recorded_model_and_implementation_hashes_match(self) -> None:
        evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))

        def digest(relative: str) -> str:
            return hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()

        self.assertEqual(evidence["model_sha256"], digest(evidence["model"]))
        self.assertEqual(evidence["config_sha256"], digest(evidence["config"]))
        for relative, expected in evidence["implementation_snapshot"]["files"].items():
            self.assertEqual(expected, digest(relative), relative)

    def test_snapshot_revision_is_an_ancestor_of_test_head(self) -> None:
        evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        revision = evidence["implementation_snapshot"]["revision"]
        result = subprocess.run(  # noqa: S603
            ["git", "merge-base", "--is-ancestor", revision, "HEAD"],  # noqa: S607
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(0, result.returncode)
        self.assertEqual(revision, evidence["implementation_snapshot"]["runtime_revision"])

    def test_contract_snapshot_matches_evidence_snapshot(self) -> None:
        evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        expected = evidence["implementation_snapshot"]
        actual = contract["implementation_snapshot"]
        self.assertEqual(expected["revision"], actual["revision"])
        self.assertEqual(expected["runtime_revision"], actual["runtime_revision"])
        self.assertIn("implementation refinement", " ".join(contract["nonclaims"]).lower())

    def test_backup_binding_is_mapped_without_refinement_overclaim(self) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        entries = contract["correspondence"]
        backup = next(
            entry
            for entry in entries
            if entry["implementation_obligation"].startswith(
                "The bounded UpgradeEngine backup phase"
            )
        )
        self.assertEqual(["BindForward", "ForwardFailure"], backup["model_actions"])
        self.assertEqual(
            "bounded executable backup binding evidence; implementation refinement pending",
            backup["status"],
        )
        for reference in backup["evidence_required"]:
            path, selector = reference.split("::", 1)
            self.assertTrue((ROOT / path).exists(), path)
            self.assertIn(selector, (ROOT / path).read_text(encoding="utf-8"), selector)

    def test_stage_binding_is_mapped_without_refinement_overclaim(self) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        entries = contract["correspondence"]
        stage = next(
            entry
            for entry in entries
            if entry["implementation_obligation"].startswith(
                "The bounded UpgradeEngine stage phase"
            )
        )
        self.assertEqual(["BindForward", "ForwardFailure"], stage["model_actions"])
        self.assertEqual(
            "bounded executable stage verification evidence; implementation refinement pending",
            stage["status"],
        )
        for reference in stage["evidence_required"]:
            path, selector = reference.split("::", 1)
            self.assertTrue((ROOT / path).exists(), path)
            self.assertIn(selector, (ROOT / path).read_text(encoding="utf-8"), selector)

    def test_selector_readiness_is_mapped_without_refinement_overclaim(self) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        entry = next(
            value
            for value in contract["correspondence"]
            if value["implementation_obligation"].startswith(
                "The bounded UpgradeEngine validate phase"
            )
        )
        self.assertEqual(["FreshRuntimeRead", "CompleteReopen"], entry["model_actions"])
        self.assertEqual(
            "bounded executable selector/runtime readiness evidence; "
            "implementation refinement pending",
            entry["status"],
        )
        for reference in entry["evidence_required"]:
            if "::" not in reference:
                continue
            path, selector = reference.split("::", 1)
            self.assertTrue((ROOT / path).exists(), path)
            self.assertIn(selector, (ROOT / path).read_text(encoding="utf-8"), selector)

    def test_selector_admission_is_mapped_without_mutation_overclaim(self) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        entry = next(
            value
            for value in contract["correspondence"]
            if value["implementation_obligation"].startswith(
                "The authority-neutral selector admission seam"
            )
        )
        self.assertEqual(
            ["ObserveAuthority", "RecheckHeld", "RejectStaleCAS"], entry["model_actions"]
        )
        self.assertEqual(
            "bounded executable selector admission evidence; implementation refinement pending",
            entry["status"],
        )
        for reference in entry["evidence_required"]:
            path, selector = reference.split("::", 1)
            self.assertTrue((ROOT / path).exists(), path)
            self.assertIn(selector, (ROOT / path).read_text(encoding="utf-8"), selector)

    def test_runtime_admission_is_mapped_without_mutation_overclaim(self) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        entry = next(
            value
            for value in contract["correspondence"]
            if value["implementation_obligation"].startswith(
                "The authority-neutral runtime admission seam"
            )
        )
        self.assertEqual(
            ["FreshRuntimeRead", "RecheckHeld", "RejectStaleCAS"], entry["model_actions"]
        )
        self.assertEqual(
            "bounded executable runtime admission evidence; implementation refinement pending",
            entry["status"],
        )
        for reference in entry["evidence_required"]:
            path, selector = reference.split("::", 1)
            self.assertTrue((ROOT / path).exists(), path)
            self.assertIn(selector, (ROOT / path).read_text(encoding="utf-8"), selector)

    def test_commit_authorization_is_mapped_without_mutation_overclaim(self) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        entry = next(
            value
            for value in contract["correspondence"]
            if value["implementation_obligation"].startswith(
                "The authority-neutral commit authorization seam"
            )
        )
        self.assertEqual(["AcceptWrite", "RejectWrite", "FinishWrite"], entry["model_actions"])
        self.assertEqual(
            "bounded executable commit authorization evidence; implementation refinement pending",
            entry["status"],
        )
        for reference in entry["evidence_required"]:
            path, selector = reference.split("::", 1)
            self.assertTrue((ROOT / path).exists(), path)
            self.assertIn(selector, (ROOT / path).read_text(encoding="utf-8"), selector)

    def test_authority_effect_journal_binds_finish_and_ambiguity_correspondence(self) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        entry = next(
            value
            for value in contract["correspondence"]
            if value["implementation_obligation"].startswith(
                "The isolated authority effect journal"
            )
        )
        self.assertEqual(
            [
                "AcceptWrite",
                "FinishWrite",
                "MarkAmbiguous",
                "RejectWrite",
                "RejectStaleCAS",
                "Acquire",
            ],
            entry["model_actions"],
        )
        self.assertEqual(
            "bounded executable durable external-effect recovery evidence; "
            "implementation refinement pending and public dispatch disabled",
            entry["status"],
        )
        for reference in entry["evidence_required"]:
            path, selector = reference.split("::", 1)
            self.assertTrue((ROOT / path).exists(), path)
            self.assertIn(
                selector.rsplit(".", 1)[-1], (ROOT / path).read_text(encoding="utf-8"), selector
            )

    def test_barrier_recheck_binds_stale_cas_correspondence(self) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        entry = next(
            value
            for value in contract["correspondence"]
            if value["implementation_obligation"].startswith(
                "A writer rereads the durable control barrier"
            )
        )
        self.assertEqual(
            ["ObserveAuthority", "RecheckHeld", "RejectStaleCAS"], entry["model_actions"]
        )
        for reference in entry["evidence_required"]:
            if "::" not in reference:
                continue
            path, selector = reference.split("::", 1)
            self.assertTrue((ROOT / path).exists(), path)
            self.assertIn(
                selector.rsplit(".", 1)[-1],
                (ROOT / path).read_text(encoding="utf-8"),
                selector,
            )

    def test_lock_domain_contract_binds_exact_hostile_evidence(self) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        entry = next(
            value
            for value in contract["correspondence"]
            if value["implementation_obligation"].startswith("One non-reentrant operation scope")
        )
        self.assertEqual(
            [
                "AcquireCommon",
                "AcquireControl",
                "AcquireAuthority",
                "ReleaseAuthority",
                "ReleaseControl",
                "ReleaseCommon",
            ],
            entry["model_actions"],
        )
        self.assertEqual("obligation-only", entry["status"])
        self.assertNotIn(
            "lock-order tests",
            entry["evidence_required"],
        )
        self.assertNotIn("re-entry rejection", entry["evidence_required"])
        self.assertNotIn("descriptor identity checks", entry["evidence_required"])
        for reference in entry["evidence_required"]:
            path, selector = reference.split("::", 1)
            self.assertTrue((ROOT / path).exists(), path)
            self.assertIn(selector.rsplit(".", 1)[-1], (ROOT / path).read_text(), selector)

    def test_recovery_evidence_is_mapped_without_authorization_overclaim(self) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        entry = next(
            value
            for value in contract["correspondence"]
            if value["implementation_obligation"].startswith("The authority-neutral recovery seam")
        )
        self.assertEqual(
            ["MarkAmbiguous", "RejectWrite", "FunctionalAvailability"], entry["model_actions"]
        )
        self.assertEqual(
            "bounded executable diagnostic recovery evidence; implementation refinement pending",
            entry["status"],
        )
        for reference in entry["evidence_required"]:
            path, selector = reference.split("::", 1)
            self.assertTrue((ROOT / path).exists(), path)
            self.assertIn(selector, (ROOT / path).read_text(encoding="utf-8"), selector)

    def test_integrated_rehearsal_is_mapped_without_mutation_overclaim(self) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        entry = next(
            value
            for value in contract["correspondence"]
            if value["implementation_obligation"].startswith("The integrated transition rehearsal")
        )
        self.assertEqual(
            ["BindForward", "ForwardFailure", "MarkAmbiguous", "FunctionalAvailability"],
            entry["model_actions"],
        )
        self.assertEqual(
            "bounded executable integrated rehearsal evidence; implementation refinement pending",
            entry["status"],
        )
        for reference in entry["evidence_required"]:
            path, selector = reference.split("::", 1)
            self.assertTrue((ROOT / path).exists(), path)
            self.assertIn(selector, (ROOT / path).read_text(encoding="utf-8"), selector)

    def test_v10_contract_binds_all_sqlite_routes_without_overclaiming_refinement(self) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        routes = contract["sqlite_route_inventory"]
        self.assertEqual(
            {
                "task_mutation",
                "observation_reconciliation",
                "command_result",
                "migration_and_retirement",
                "fresh_install_baseline",
            },
            {entry["route"] for entry in routes},
        )
        for entry in routes:
            for field in ("entrypoint", "binding", "evidence"):
                path, selector = entry[field].split("::", 1)
                self.assertTrue((ROOT / path).exists(), path)
                self.assertIn(selector, (ROOT / path).read_text(encoding="utf-8"), selector)
        self.assertEqual("not-proven", contract["refinement_boundary"]["implementation_refinement"])

    def test_v10_contract_binds_process_death_evidence_without_refinement_overclaim(self) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        evidence = contract["process_death_evidence"]
        self.assertGreaterEqual(len(evidence), 6)
        required_actions = {
            "MarkAmbiguous",
            "AmbiguousIsWriteClosed",
            "RejectStaleCAS",
            "LockOwnership",
            "Acquire",
            "RecheckHeld",
        }
        observed_actions = {action for entry in evidence for action in entry["model_actions"]}
        self.assertTrue(required_actions <= observed_actions)
        for entry in evidence:
            path, selector = entry["implementation_test"].split("::", 1)
            self.assertTrue((ROOT / path).exists(), path)
            self.assertIn(selector, (ROOT / path).read_text(encoding="utf-8"), selector)
        self.assertEqual("not-proven", contract["refinement_boundary"]["implementation_refinement"])


if __name__ == "__main__":
    unittest.main()
