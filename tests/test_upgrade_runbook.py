# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Hostile tests for privacy-safe release runbook generation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tools.generate_upgrade_contract import generate
from tools.generate_upgrade_runbook import RunbookError, generate_runbooks, main, write_runbooks
from tools.verify_upgrade_runbook import (
    RunbookVerificationError,
    verify_runbooks,
)
from tools.verify_upgrade_runbook import (
    main as verify_main,
)


def _release(version: str, seed: str) -> dict[str, str]:
    return {
        "version": version,
        "source_commit": seed * 40,
        "tag_ref": f"refs/tags/{version}",
        "tag_object": chr(ord(seed) + 1) * 40,
        "signature_sha256": chr(ord(seed) + 2) * 64,
        "trust_policy_sha256": chr(ord(seed) + 3) * 64,
        "vendor_manifest_sha256": chr(ord(seed) + 4) * 64,
    }


def _contract() -> dict[str, Any]:
    return generate(
        {
            "operation_id": "upgrade:v0.3.5-to-v0.3.6:001",
            "backend": "sqlite",
            "selector_ref": ".runtime/runtime-selector.json",
            "expected_state_revision": 7,
            "barrier_id": "barrier-7",
            "fencing_token": "fence-7",
            "from": _release("v0.3.5", "a"),
            "to": _release("v0.3.6", "b"),
        }
    )


class UpgradeRunbookTests(unittest.TestCase):
    def test_release_workflow_revalidates_from_fresh_clone(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1] / ".github/workflows/release.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('git clone --no-local --no-hardlinks "$GITHUB_WORKSPACE"', workflow)
        self.assertIn('uv run --directory "$RUNNER_TEMP/release-clone"', workflow)
        self.assertIn('cmp -- "$RUNNER_TEMP/release-contract.json"', workflow)
        self.assertIn('cmp -- "$RUNNER_TEMP/release-runbooks/operator.md"', workflow)
        self.assertIn('cmp -- "$RUNNER_TEMP/release-runbooks/agent.md"', workflow)

    def test_release_workflow_binds_artifact_digests_to_source(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1] / ".github/workflows/release.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'if [[ ! "$GITHUB_SHA" =~ ^[0-9a-f]{40}$ ]]; then',
            workflow,
        )
        self.assertIn('echo "GITHUB_SHA is not a 40-hex source identity"', workflow)
        self.assertIn("printf '%s\\n' \"$GITHUB_SHA\"", workflow)
        self.assertIn(
            "sha256sum release-contract.json release-runbooks/operator.md "
            "release-runbooks/agent.md",
            workflow,
        )
        self.assertIn("release-source-commit.txt", workflow)
        self.assertIn("release-artifacts.sha256", workflow)

    def test_release_workflow_generates_and_verifies_sanitized_runbooks(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1] / ".github/workflows/release.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("tools.generate_upgrade_runbook", workflow)
        self.assertIn("tools.verify_upgrade_runbook", workflow)
        self.assertIn("${{ runner.temp }}/release-runbooks", workflow)
        self.assertIn("if-no-files-found: error", workflow)

    def test_checked_in_release_fixture_matches_generator_and_is_private(self) -> None:
        root = Path(__file__).resolve().parents[1]
        fixture = root / "examples/upgrade/fixture-v0.3.8-to-v0.3.9"
        document = json.loads(
            (root / "examples/upgrade/fixture-v0.3.8-to-v0.3.9.contract.json").read_text(
                encoding="utf-8"
            )
        )
        generated = generate_runbooks(document)
        for name, content in generated.items():
            self.assertEqual(content, (fixture / name).read_text(encoding="utf-8"))
            self.assertNotIn(document["phases"][0]["operation"]["inputs"]["selector_ref"], content)
            self.assertNotIn(document["phases"][0]["operation"]["inputs"]["barrier_id"], content)
            self.assertNotIn(document["phases"][0]["operation"]["inputs"]["fencing_token"], content)
            for release in (document["from"], document["to"]):
                for field in (
                    "source_commit",
                    "tag_object",
                    "signature_sha256",
                    "trust_policy_sha256",
                    "vendor_manifest_sha256",
                ):
                    self.assertNotIn(release[field], content)

        verify_runbooks(document, fixture)

    def test_checker_rejects_stale_or_aliased_output(self) -> None:
        document = _contract()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "runbooks"
            write_runbooks(document, output)
            (output / "agent.md").write_text("stale", encoding="utf-8")
            with self.assertRaises(RunbookVerificationError):
                verify_runbooks(document, output)
            write_runbooks(document, output)
            (output / "stale.md").write_text("old output", encoding="utf-8")
            with self.assertRaises(RunbookVerificationError):
                verify_runbooks(document, output)
            (output / "stale.md").unlink()
            target = root / "target.md"
            target.write_text((output / "agent.md").read_text(encoding="utf-8"), encoding="utf-8")
            (output / "agent.md").unlink()
            (output / "agent.md").symlink_to(target)
            with self.assertRaises(RunbookVerificationError):
                verify_runbooks(document, output)

    def test_checker_cli_and_generator_cli_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contract = root / "contract.json"
            output = root / "runbooks"
            contract.write_text("not-json", encoding="utf-8")
            self.assertEqual(1, verify_main([str(contract), str(output)]))
            self.assertEqual(1, main([str(contract), str(output)]))
            contract.write_text("[]", encoding="utf-8")
            self.assertEqual(1, main([str(contract), str(output)]))
            contract.write_text(json.dumps(_contract()), encoding="utf-8")
            self.assertEqual(1, verify_main([str(contract), str(output)]))

    def test_git_release_runbook_uses_git_backup_instructions(self) -> None:
        document = _contract()
        document["backend"] = "git"
        for phase in document["phases"]:
            phase["operation"]["inputs"]["backend"] = "git"
        document["rollback"]["operation"]["inputs"]["backend"] = "git"
        generated = generate_runbooks(document)
        self.assertIn("reachable objects", generated["operator.md"])
        self.assertNotIn("WAL/SHM", generated["operator.md"])

    def test_generation_is_deterministic_and_contains_safety_gates(self) -> None:
        first = generate_runbooks(_contract())
        self.assertEqual(first, generate_runbooks(_contract()))
        self.assertEqual(set(first), {"operator.md", "agent.md"})
        for text in first.values():
            self.assertIn("work closed", text)
            self.assertIn("health check", text)
            self.assertIn("reconcile", text)
            self.assertIn("WAL/SHM", text)
        self.assertIn("backend.restore", first["operator.md"])
        self.assertIn("barrier.acquire", first["operator.md"])

    def test_output_excludes_private_contract_values(self) -> None:
        document = _contract()
        rendered = "\n".join(generate_runbooks(document).values())
        for value in (
            document["phases"][0]["operation"]["inputs"]["selector_ref"],
            document["phases"][0]["operation"]["inputs"]["barrier_id"],
            document["phases"][0]["operation"]["inputs"]["fencing_token"],
            document["from"]["source_commit"],
            document["to"]["signature_sha256"],
        ):
            self.assertNotIn(value, rendered)

    def test_write_and_cli_require_valid_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contract = root / "contract.json"
            output = root / "runbooks"
            contract.write_text(json.dumps(_contract()), encoding="utf-8")
            self.assertEqual(0, main([str(contract), str(output)]))
            self.assertEqual(
                sorted((output / name).name for name in ("operator.md", "agent.md")),
                ["agent.md", "operator.md"],
            )
            before = sorted(path.name for path in output.iterdir())
            contract.write_text("{}", encoding="utf-8")
            with self.assertRaises(RunbookError):
                write_runbooks(json.loads(contract.read_text()), output / "invalid")
            self.assertEqual(before, sorted(path.name for path in output.iterdir()))


if __name__ == "__main__":
    unittest.main()
