# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Tests for deterministic offline coordinator vendoring."""

import importlib.util
import json
import os
import re
import runpy
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import patch

from tools.generate_upgrade_contract import generate

ROOT = Path(__file__).resolve().parent.parent
CURRENT_VERSION = f"v{tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']}"
SOURCE = ROOT / "tools/vendor.py"
SPEC = importlib.util.spec_from_file_location("handoffctl_vendor", SOURCE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load vendor tool")
VENDOR: Any = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VENDOR)


def upgrade_contract(backend: str) -> dict[str, Any]:
    def release(version: str, seed: str) -> dict[str, str]:
        return {
            "version": version,
            "source_commit": seed * 40,
            "tag_ref": f"refs/tags/{version}",
            "tag_object": chr(ord(seed) + 1) * 40,
            "signature_sha256": chr(ord(seed) + 2) * 64,
            "trust_policy_sha256": chr(ord(seed) + 3) * 64,
            "vendor_manifest_sha256": chr(ord(seed) + 4) * 64,
        }

    return generate(
        {
            "operation_id": "upgrade:v0.3.5-to-v0.3.6:001",
            "backend": backend,
            "selector_ref": ".runtime/runtime-selector.json",
            "expected_state_revision": 7,
            "barrier_id": "barrier-7",
            "fencing_token": "fence-7",
            "from": release("v0.3.5", "a"),
            "to": release(CURRENT_VERSION, "b"),
        }
    )


class VendorTest(unittest.TestCase):
    def test_formal_attestation_binds_release_and_development_vendor_identity(self) -> None:
        candidate_identity = runpy.run_path(str(ROOT / "formal/handoffctl/attest.py"))[
            "candidate_identity"
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "coordinator.vendor.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "upstream": {
                            "repository": VENDOR.UPSTREAM_REPOSITORY,
                            "version": CURRENT_VERSION,
                            "commit": "a" * 40,
                        },
                        "files": {},
                    },
                    sort_keys=True,
                )
            )
            release = candidate_identity(root)
            self.assertEqual(
                ("release-vendor", "a" * 40, ""),
                (
                    release["kind"],
                    release["commit"],
                    release["tree"],
                ),
            )
            self.assertEqual(VENDOR.sha256(manifest), release["vendor_manifest_sha256"])

            value = json.loads(manifest.read_text())
            value["schema_version"] = 2
            value["upstream"].update(channel="development", tree="b" * 40)
            manifest.write_text(json.dumps(value, sort_keys=True))
            development = candidate_identity(root)
            self.assertEqual(
                ("development-vendor", "a" * 40, "b" * 40),
                (development["kind"], development["commit"], development["tree"]),
            )
            value["upstream"]["tree"] = "bad"
            manifest.write_text(json.dumps(value, sort_keys=True))
            with self.assertRaisesRegex(ValueError, "exact development vendor identity"):
                candidate_identity(root)

    def create_development_source(self, name: str) -> tuple[Path, str]:
        source = Path(self.temporary.name) / name
        (source / "tools").mkdir(parents=True)
        version = CURRENT_VERSION.removeprefix("v")
        (source / "tools/handoffctl.py").write_text(
            f'COORDINATOR_VERSION = "{version}"\n', encoding="utf-8"
        )
        (source / "LICENSE").write_text("committed-license\n", encoding="utf-8")
        subprocess.run(["/usr/bin/git", "init", "-q"], cwd=source, check=True)
        subprocess.run(["/usr/bin/git", "add", "."], cwd=source, check=True)
        subprocess.run(
            [
                "/usr/bin/git",
                "-c",
                "user.name=Vendor Test",
                "-c",
                "user.email=vendor-test@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
            cwd=source,
            check=True,
        )
        commit = subprocess.run(
            ["/usr/bin/git", "rev-parse", "HEAD"],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return source, commit

    def test_snapshot_contains_shared_formal_verifier_helper_closure(self) -> None:
        verifier = (ROOT / "formal/handoffctl/verify.sh").read_text(encoding="utf-8")
        executed_tools = set(
            re.findall(r"\$\{SPEC_DIR\}/\.\./\.\./(tools/[A-Za-z0-9_./-]+\.py)", verifier)
        )
        sources = {source for source, _ in VENDOR.SOURCE_FILES}
        self.assertEqual({"tools/tlc_runner.py"}, executed_tools)
        self.assertTrue(executed_tools.issubset(sources))
        self.assertIn("tests/test_tlc_runner.py", sources)

    def test_snapshot_contains_complete_first_party_formal_closure(self) -> None:
        sources = {source for source, _ in VENDOR.SOURCE_FILES}
        expected = {
            "formal/evidence.json",
            "formal/tier-evidence.json",
            "formal/handoffctl/Handoffctl.cfg",
            "formal/handoffctl/Handoffctl.tla",
            "formal/handoffctl/HandoffctlBinding.cfg",
            "formal/handoffctl/HandoffctlBinding.tla",
            "formal/handoffctl/HandoffctlFast.cfg",
            "formal/handoffctl/HandoffctlLocks.cfg",
            "formal/handoffctl/HandoffctlLocks.tla",
            "formal/handoffctl/HandoffctlPR.cfg",
            "formal/handoffctl/HandoffctlRecovery.cfg",
            "formal/handoffctl/HandoffctlRecovery.tla",
            "formal/handoffctl/HandoffctlRun.cfg",
            "formal/handoffctl/HandoffctlRun.tla",
            "formal/handoffctl/HandoffctlStorage.cfg",
            "formal/handoffctl/HandoffctlStorage.tla",
            "formal/handoffctl/attest.py",
            "formal/handoffctl/verify.sh",
            "formal/oracle/OracleInteractionGates.cfg",
            "formal/oracle/OracleInteractionGates.tla",
            "tools/tlc_runner.py",
        }
        self.assertTrue(expected.issubset(sources))

    def test_vendor_verify_rejects_stale_lifecycle_model_even_with_matching_digest(self) -> None:
        with patch("builtins.print"):
            VENDOR.sync(ROOT, self.target, CURRENT_VERSION, "1" * 40)
        model = self.target / "formal/handoffctl/Handoffctl.tla"
        model.write_text(model.read_text().replace('"resume", ', "", 1))
        manifest_path = self.target / VENDOR.LOCK_NAME
        manifest = json.loads(manifest_path.read_text())
        manifest["files"]["formal/handoffctl/Handoffctl.tla"]["sha256"] = VENDOR.sha256(model)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        with self.assertRaisesRegex(RuntimeError, "formal lifecycle operation drift"):
            VENDOR.verify(self.target)

    def test_formal_lifecycle_alignment_rejects_malformed_and_missing_inventories(self) -> None:
        tools = self.target / "tools"
        formal = self.target / "formal/handoffctl"
        tools.mkdir(parents=True)
        formal.mkdir(parents=True)
        runtime = tools / "handoffctl.py"
        model = formal / "Handoffctl.tla"

        runtime.write_text('LIFECYCLE_MUTATION_COMMANDS = ("resume", 3)\n')
        model.write_text("Operations == {resume}\n")
        with self.assertRaisesRegex(RuntimeError, "runtime inventory"):
            VENDOR.verify_formal_lifecycle_alignment(self.target)

        runtime.write_text("not valid python !\n")
        with self.assertRaisesRegex(RuntimeError, "inputs are unreadable"):
            VENDOR.verify_formal_lifecycle_alignment(self.target)

        runtime.write_text('LIFECYCLE_MUTATION_COMMANDS = ("resume",)\n')
        with self.assertRaisesRegex(RuntimeError, "operation inventory"):
            VENDOR.verify_formal_lifecycle_alignment(self.target)

    def test_empty_destination_runs_vendored_portable_and_publication_tiers(self) -> None:
        with patch("builtins.print"):
            VENDOR.sync(ROOT, self.target, CURRENT_VERSION, "1" * 40)
        fixture_bin = self.target / "formal-fixture-bin"
        fixture_bin.mkdir()
        scripts = {
            "curl": """#!/bin/sh
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--output" ]; then
    shift
    printf 'fixture-jar' > "$1"
    exit 0
  fi
  shift
done
exit 2
""",
            "sha256sum": "#!/bin/sh\nexit 0\n",
            "java": "#!/bin/sh\nexit 0\n",
            "systemd-run": """#!/bin/sh
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done
[ "$#" -gt 0 ] && shift
exec "$@"
""",
        }
        for name, content in scripts.items():
            path = fixture_bin / name
            path.write_text(content)
            path.chmod(0o755)
        environment = {
            **os.environ,
            "PATH": str(fixture_bin) + os.pathsep + os.environ["PATH"],
            "TLC_CGROUP_MODE": "required",
            "TLC_HEAP": "512m",
            "TLC_MEMORY_MAX": "3G",
            "TLC_SWAP_MAX": "3G",
            "TLC_TIMEOUT_SECONDS": "30",
        }
        expected_models = {
            "portable-smoke": {"HandoffctlBinding"},
            "pr-publication": {
                "HandoffctlBinding",
                "HandoffctlLocks",
                "HandoffctlRun",
                "HandoffctlStorage",
                "HandoffctlPR",
                "HandoffctlRecovery",
            },
        }
        for tier, models in expected_models.items():
            attestation = self.target / f"{tier}-attestation.json"
            queue = self.target / f"{tier}-queue"
            admission_lock = self.target / f"{tier}-admission.lock"
            result = subprocess.run(  # noqa: S603 - exact vendored executable under test
                [
                    str(self.target / "formal/handoffctl/verify.sh"),
                    "--tier",
                    tier,
                    "--diagnostic-queue",
                    str(queue),
                    "--diagnostic-admission-lock",
                    str(admission_lock),
                ],
                cwd=self.target,
                check=False,
                capture_output=True,
                text=True,
                env={**environment, "TLC_ATTESTATION_PATH": str(attestation)},
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            value = json.loads(attestation.read_text())
            self.assertEqual(models, set(value["models"]))
            self.assertEqual("release-vendor", value["candidate_identity"]["kind"])
            self.assertEqual("1" * 40, value["candidate_identity"]["commit"])
            self.assertEqual("diagnostic-private-admission", value["execution_classification"])
            self.assertFalse(value["canonical_publication_evidence"])
            self.assertTrue(
                any(
                    "cannot support release or publication claims" in item
                    for item in value["non_claims"]
                )
            )
            self.assertEqual(
                VENDOR.sha256(self.target / VENDOR.LOCK_NAME),
                value["candidate_identity"]["vendor_manifest_sha256"],
            )
            for name in (
                "formal/evidence.json",
                "formal/tier-evidence.json",
                "formal/handoffctl/verify.sh",
                "formal/handoffctl/attest.py",
                "tools/tlc_runner.py",
            ):
                self.assertEqual(VENDOR.sha256(self.target / name), value["formal_inputs"][name])

    def test_verify_diagnostic_admission_requires_both_distinct_absolute_paths(self) -> None:
        script = (ROOT / "formal/handoffctl/verify.sh").read_text(encoding="utf-8")
        documentation = (ROOT / "formal/handoffctl/README.md").read_text(encoding="utf-8")
        self.assertIn('[[ "$2" != /* || "$4" != /* || "$2" == "$4" ]]', script)
        self.assertIn("diagnostic queue and admission lock must be supplied together", script)
        self.assertIn("--execution-classification", script)
        self.assertIn("diagnostic-private-admission", documentation)
        self.assertIn("cannot support publication or release claims", documentation)

    def test_snapshot_contains_runtime_mutation_dependency_closure(self) -> None:
        sources = {source for source, _ in VENDOR.SOURCE_FILES}
        self.assertTrue(
            {
                "tools/lifecycle_trace.py",
                "tools/board_metrics.py",
                "tools/lock_domain.py",
                "tools/lock_domain_scope.py",
                "tools/mutation_fence.py",
                "tools/rollback_control_store.py",
                "tools/sqlite_wal_lifecycle.py",
                "tools/rollback_evidence.py",
                "tools/upgrade_identity.py",
            }.issubset(sources)
        )

    def test_synced_runtime_passes_full_initialized_state_validation(self) -> None:
        product = Path(self.temporary.name) / "product"
        product.mkdir()
        repositories = (
            (self.target, "https://github.com/owner/state.git"),
            (product, "https://github.com/owner/product.git"),
        )
        for repository, remote in repositories:
            subprocess.run(
                ["/usr/bin/git", "init", "-q"],
                cwd=repository,
                check=True,
                capture_output=True,
            )
            subprocess.run(  # noqa: S603
                ["/usr/bin/git", "remote", "add", "origin", remote],
                cwd=repository,
                check=True,
                capture_output=True,
            )
        with patch("builtins.print"):
            VENDOR.sync(ROOT, self.target, CURRENT_VERSION, "e" * 40)
        imported = subprocess.run(
            [
                sys.executable,
                "-S",
                "-c",
                "from tools.admission_lease import AdmissionLease\n"
                "from tools.runtime_bootstrap import resolve_selected_runtime_bound\n"
                "from tools.upgrade_authority import read_runtime_selector\n"
                "assert (AdmissionLease and resolve_selected_runtime_bound and "
                "read_runtime_selector)",
            ],
            cwd=self.target,
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": os.environ["PATH"], "PYTHONPATH": str(self.target)},
        )
        self.assertEqual(imported.returncode, 0, imported.stdout + imported.stderr)
        initialization_command = [
            sys.executable,
            str(self.target / "tools/handoffctl.py"),
            "init",
            "--state-repository",
            "owner/state",
            "--product-repository",
            "owner/product",
            "--project-name",
            "test-project",
            "--project-title",
            "Test Project",
            "--backend",
            "git",
        ]
        initialized = subprocess.run(  # noqa: S603
            initialization_command, cwd=self.target, check=False, capture_output=True, text=True
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        runtime = self.target / ".runtime"
        runtime.mkdir()
        (runtime / "config.json").write_text(
            json.dumps(
                {
                    "projects_root": self.temporary.name,
                    "product_worktree": "product",
                    "github_repository": "owner/product",
                    "push_enabled": False,
                }
            )
        )
        doctor = subprocess.run(  # noqa: S603
            [sys.executable, str(self.target / "tools/handoffctl.py"), "doctor"],
            cwd=self.target,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIn("privacy", doctor.stdout)
        help_result = subprocess.run(  # noqa: S603
            [sys.executable, "-S", str(self.target / "tools/handoffctl.py"), "--help"],
            cwd=self.target,
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": os.environ["PATH"], "PYTHONPATH": str(self.target)},
        )
        self.assertEqual(0, help_result.returncode, help_result.stderr)
        self.assertIn("unblock", help_result.stdout)
        embedded_guide = (self.target / "docs/agent-workflow-coordinator.md").read_text()
        self.assertIn("tools/handoffctl unblock", embedded_guide)

        document = upgrade_contract("git")
        contract_path = Path(self.temporary.name) / "upgrade-contract.json"
        contract_path.write_text(json.dumps(document), encoding="utf-8")
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

        def state_bytes() -> dict[str, bytes]:
            return {
                str(path.relative_to(self.target)): path.read_bytes()
                for path in self.target.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts
            }

        before = state_bytes()
        for action in ("check", "plan"):
            upgrade_result = subprocess.run(  # noqa: S603
                [
                    sys.executable,
                    "-S",
                    str(self.target / "tools/handoffctl.py"),
                    "upgrade",
                    action,
                    "--contract",
                    str(contract_path),
                ],
                cwd=self.target,
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(0, upgrade_result.returncode, upgrade_result.stderr)
            report = json.loads(upgrade_result.stdout)
            self.assertFalse(report["executable"])
            self.assertEqual("git", report["backend"])
            self.assertEqual(before, state_bytes())
        for action in ("apply", "rollback"):
            upgrade_result = subprocess.run(  # noqa: S603
                [
                    sys.executable,
                    "-S",
                    str(self.target / "tools/handoffctl.py"),
                    "upgrade",
                    action,
                    "--contract",
                    str(contract_path),
                ],
                cwd=self.target,
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(1, upgrade_result.returncode)
            self.assertIn("no coordinator state was mutated", upgrade_result.stderr)
            self.assertEqual(before, state_bytes())

    def test_synced_snapshot_obeys_shebang_and_executable_mode_policy(self) -> None:
        with patch("builtins.print"):
            VENDOR.sync(ROOT, self.target, CURRENT_VERSION, "f" * 40)

        def mismatches() -> list[str]:
            invalid = []
            for _, destination in VENDOR.SOURCE_FILES:
                path = self.target / destination
                has_shebang = path.read_bytes().startswith(b"#!")
                is_executable = bool(path.stat().st_mode & 0o111)
                if has_shebang != is_executable:
                    invalid.append(destination)
            return invalid

        self.assertEqual([], mismatches())
        storage = self.target / "tools/sqlite_storage.py"
        self.assertEqual(
            [
                "# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.",
                "# SPDX-License-Identifier: MIT",
            ],
            storage.read_text().splitlines()[:2],
        )
        self.assertFalse(storage.stat().st_mode & 0o111)
        storage.write_text("#!/usr/bin/env python3\n" + storage.read_text())
        storage.chmod(0o644)
        self.assertEqual(["tools/sqlite_storage.py"], mismatches())

    def test_synced_formal_runner_executes_from_clean_destination(self) -> None:
        with patch("builtins.print"):
            VENDOR.sync(ROOT, self.target, CURRENT_VERSION, "1" * 40)
        fixture_bin = self.target / "fixture-bin"
        fixture_bin.mkdir()
        java = fixture_bin / "java"
        java.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        java.chmod(0o755)
        for name in ("runner.jar", "Model.tla", "Model.cfg"):
            (self.target / name).write_text("fixture\n", encoding="utf-8")
        result = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-S",
                str(self.target / "tools/tlc_runner.py"),
                "--jar",
                str(self.target / "runner.jar"),
                "--model",
                str(self.target / "Model.tla"),
                "--config",
                str(self.target / "Model.cfg"),
                "--metadir",
                str(self.target / "states"),
                "--queue",
                str(self.target / "queue"),
                "--admission-lock",
                str(self.target / "admission.lock"),
                "--cgroup-mode",
                "off",
            ],
            cwd=self.target,
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": str(fixture_bin), "PYTHONPATH": str(self.target)},
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        outcomes = list((self.target / "queue").glob("*.outcome.json"))
        self.assertEqual(1, len(outcomes))
        self.assertEqual("completed", json.loads(outcomes[0].read_text())["state"])
        runner = self.target / "tools/tlc_runner.py"
        original_runner = runner.read_bytes()
        runner.unlink()
        with self.assertRaisesRegex(RuntimeError, "regular file: .*tools/tlc_runner.py"):
            VENDOR.verify(self.target)
        runner.write_bytes(original_runner)
        runner.write_text("substituted\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "digest mismatch: tools/tlc_runner.py"):
            VENDOR.verify(self.target)

    def test_runtime_version_matches_project_metadata(self) -> None:
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
        version = metadata["project"]["version"]
        runtime = (ROOT / "tools/handoffctl.py").read_text()
        self.assertIn(f'COORDINATOR_VERSION = "{version}"', runtime)

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.target = Path(self.temporary.name) / "target"
        self.target.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_sync_verify_and_detect_tampering(self) -> None:
        commit = "a" * 40
        profile = self.target / ".handoffctl.json"
        binding = self.target / "coordinator.binding.json"
        backend = self.target / "coordinator.backend.json"
        profile.write_text("project-profile-sentinel\n")
        binding.write_text("project-binding-sentinel\n")
        backend.write_text("backend-selection-sentinel\n")
        with patch("builtins.print") as output:
            VENDOR.sync(ROOT, self.target, CURRENT_VERSION, commit)
        output.assert_called_once()
        self.assertEqual("project-profile-sentinel\n", profile.read_text())
        self.assertEqual("project-binding-sentinel\n", binding.read_text())
        self.assertEqual("backend-selection-sentinel\n", backend.read_text())
        lock = json.loads((self.target / VENDOR.LOCK_NAME).read_text())
        self.assertEqual(1, lock["schema_version"])
        self.assertEqual({"repository", "version", "commit"}, set(lock["upstream"]))
        self.assertEqual(commit, lock["upstream"]["commit"])
        self.assertEqual(
            {destination for _, destination in VENDOR.SOURCE_FILES}, set(lock["files"])
        )
        self.assertTrue(os.access(self.target / "tools/handoffctl", os.X_OK))
        with patch("builtins.print"):
            VENDOR.verify(self.target)
        (self.target / "tools/handoffctl.py").write_text("tampered\n")
        with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
            VENDOR.verify(self.target)

    def test_clean_development_closure_passes_strict_privacy_scan(self) -> None:
        """A clean exact sync has no scanner residue; an injected UUID still fails."""
        commit = "c" * 40
        tree = "d" * 40

        def payload(_source: Path, _commit: str, source_name: str) -> tuple[bytes, int]:
            path = ROOT / source_name
            return path.read_bytes(), path.stat().st_mode & 0o777

        with (
            patch.object(VENDOR, "development_identity", return_value=tree),
            patch.object(VENDOR, "git_blob_payload", side_effect=payload),
            patch("builtins.print"),
        ):
            VENDOR.sync_development(ROOT, self.target, commit)
        with patch("builtins.print"):
            VENDOR.verify(self.target)
        self.assertFalse(any(path.is_symlink() for path in self.target.rglob("*")))
        self.assertFalse(
            any(
                path.name == "__pycache__" or path.suffix in {".pyc", ".pyo"}
                for path in self.target.rglob("*")
            )
        )
        self.assertFalse(
            any(path.stat().st_size > 200_000 for path in self.target.rglob("*") if path.is_file())
        )
        self.assertFalse(
            any(path.suffix in {".log", ".transcript"} for path in self.target.rglob("*"))
        )

        runtime_spec = importlib.util.spec_from_file_location(
            "vendored_handoffctl_privacy", self.target / "tools/handoffctl.py"
        )
        if runtime_spec is None or runtime_spec.loader is None:
            self.fail("cannot load vendored runtime")
        sys.path.insert(0, str(self.target / "tools"))
        try:
            runtime = cast(Any, importlib.util.module_from_spec(runtime_spec))
            runtime_spec.loader.exec_module(runtime)
            runtime.ROOT = self.target
            self.assertEqual([], runtime.privacy_errors())
            leaked = self.target / "leaked-fixture.txt"
            leaked.write_text("11111111-1111-4111-8111-111111111111\n", encoding="utf-8")
            self.assertEqual(["leaked-fixture.txt: session-like UUID"], runtime.privacy_errors())
        finally:
            sys.path.remove(str(self.target / "tools"))

    def test_rejects_bad_release_identity_and_lock_shapes(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "invalid release"):
            VENDOR.build_lock(ROOT, "latest", "short")
        (self.target / VENDOR.LOCK_NAME).write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "lock structure"):
            VENDOR.verify(self.target)
        (self.target / VENDOR.LOCK_NAME).write_text("not-json")
        with self.assertRaisesRegex(RuntimeError, "cannot read"):
            VENDOR.verify(self.target)

    def test_release_identity_requires_clean_exact_tag(self) -> None:
        with patch.object(VENDOR, "git_output", side_effect=["", "b" * 40, CURRENT_VERSION]):
            self.assertEqual("b" * 40, VENDOR.release_identity(ROOT, CURRENT_VERSION))
        with (
            patch.object(VENDOR, "git_output", return_value="dirty"),
            self.assertRaisesRegex(RuntimeError, "must be clean"),
        ):
            VENDOR.release_identity(ROOT, CURRENT_VERSION)
        with (
            patch.object(VENDOR, "git_output", side_effect=["", "b" * 40, "v0.4.0"]),
            self.assertRaisesRegex(RuntimeError, "not tagged"),
        ):
            VENDOR.release_identity(ROOT, CURRENT_VERSION)
        with self.assertRaisesRegex(RuntimeError, "form vMAJOR"):
            VENDOR.release_identity(ROOT, "main")

    def test_development_identity_requires_clean_exact_head_and_tree(self) -> None:
        commit = "c" * 40
        tree = "d" * 40
        root = str(ROOT.resolve())
        with patch.object(VENDOR, "git_output", side_effect=[root, "", commit, tree]):
            self.assertEqual(tree, VENDOR.development_identity(ROOT, commit))
        with (
            patch.object(VENDOR, "git_output", side_effect=[root, "dirty"]),
            self.assertRaisesRegex(RuntimeError, "must be clean"),
        ):
            VENDOR.development_identity(ROOT, commit)
        with (
            patch.object(VENDOR, "git_output", side_effect=[root, "", "e" * 40]),
            self.assertRaisesRegex(RuntimeError, "HEAD differs"),
        ):
            VENDOR.development_identity(ROOT, commit)
        with (
            patch.object(VENDOR, "git_output", side_effect=[root, "", commit, "short"]),
            self.assertRaisesRegex(RuntimeError, "tree is not a full"),
        ):
            VENDOR.development_identity(ROOT, commit)
        with (
            patch.object(VENDOR, "git_output", return_value=str(ROOT.parent)),
            self.assertRaisesRegex(RuntimeError, "worktree root"),
        ):
            VENDOR.development_identity(ROOT, commit)
        with self.assertRaisesRegex(RuntimeError, "full commit"):
            VENDOR.development_identity(ROOT, "short")

    def test_sync_development_records_distinct_exact_identity(self) -> None:
        commit = "c" * 40
        tree = "d" * 40

        def payload(_source: Path, _commit: str, source_name: str) -> tuple[bytes, int]:
            path = ROOT / source_name
            return path.read_bytes(), path.stat().st_mode & 0o777

        with (
            patch.object(VENDOR, "development_identity", return_value=tree),
            patch.object(VENDOR, "git_blob_payload", side_effect=payload),
            patch("builtins.print"),
        ):
            VENDOR.sync_development(ROOT, self.target, commit)
        lock_path = self.target / VENDOR.LOCK_NAME
        lock = json.loads(lock_path.read_text())
        self.assertEqual(2, lock["schema_version"])
        self.assertEqual(
            {"repository", "version", "commit", "channel", "tree"}, set(lock["upstream"])
        )
        self.assertEqual("development", lock["upstream"]["channel"])
        self.assertEqual(commit, lock["upstream"]["commit"])
        self.assertEqual(tree, lock["upstream"]["tree"])
        with patch("builtins.print"):
            VENDOR.verify(self.target)
        for field, value in (("channel", "release"), ("tree", "short")):
            invalid = json.loads(json.dumps(lock))
            invalid["upstream"][field] = value
            lock_path.write_text(json.dumps(invalid), encoding="utf-8")
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(RuntimeError, "invalid development"),
            ):
                VENDOR.verify(self.target)
        lock["schema_version"] = 3
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "unsupported vendor lock schema"):
            VENDOR.verify(self.target)

    def test_development_sync_ignores_assume_unchanged_worktree_substitution(self) -> None:
        source, commit = self.create_development_source("assume-unchanged-source")
        (source / "LICENSE").write_text("substituted-worktree-license\n", encoding="utf-8")
        subprocess.run(
            ["/usr/bin/git", "update-index", "--assume-unchanged", "LICENSE"],
            cwd=source,
            check=True,
        )
        sources = (
            ("tools/handoffctl.py", "tools/handoffctl.py"),
            ("LICENSE", "LICENSE"),
        )
        with patch.object(VENDOR, "SOURCE_FILES", sources), patch("builtins.print"):
            VENDOR.sync_development(source, self.target, commit)
        self.assertEqual("committed-license\n", (self.target / "LICENSE").read_text())

    def test_development_sync_uses_committed_runtime_version(self) -> None:
        source, commit = self.create_development_source("hidden-version-source")
        subprocess.run(
            ["/usr/bin/git", "update-index", "--assume-unchanged", "tools/handoffctl.py"],
            cwd=source,
            check=True,
        )
        (source / "tools/handoffctl.py").write_text(
            'COORDINATOR_VERSION = "9.9.9"\n', encoding="utf-8"
        )
        sources = (
            ("tools/handoffctl.py", "tools/handoffctl.py"),
            ("LICENSE", "LICENSE"),
        )
        with patch.object(VENDOR, "SOURCE_FILES", sources), patch("builtins.print"):
            VENDOR.sync_development(source, self.target, commit)
        lock = json.loads((self.target / VENDOR.LOCK_NAME).read_text())
        self.assertEqual(CURRENT_VERSION, lock["upstream"]["version"])
        runtime_version = CURRENT_VERSION.removeprefix("v")
        expected = 'COORDINATOR_VERSION = "' + runtime_version + '"'
        self.assertIn(expected, (self.target / "tools/handoffctl.py").read_text())

    def test_development_sync_ignores_mid_operation_worktree_substitution(self) -> None:
        source, commit = self.create_development_source("mid-operation-source")
        original = VENDOR.git_blob_payload
        calls = 0

        def mutate_then_read(root: Path, exact_commit: str, name: str) -> tuple[bytes, int]:
            nonlocal calls
            calls += 1
            if calls == 1:
                (root / "LICENSE").write_text("mid-operation-substitution\n", encoding="utf-8")
            return cast(tuple[bytes, int], original(root, exact_commit, name))

        sources = (
            ("tools/handoffctl.py", "tools/handoffctl.py"),
            ("LICENSE", "LICENSE"),
        )
        with (
            patch.object(VENDOR, "SOURCE_FILES", sources),
            patch.object(VENDOR, "git_blob_payload", side_effect=mutate_then_read),
            patch("builtins.print"),
        ):
            VENDOR.sync_development(source, self.target, commit)
        self.assertEqual("committed-license\n", (self.target / "LICENSE").read_text())

    def test_snapshot_verification_failure_preserves_existing_target(self) -> None:
        destination = self.target / "LICENSE"
        destination.write_text("existing-target\n", encoding="utf-8")
        manifest = {"schema_version": 1, "upstream": {}, "files": {}}
        with (
            patch.object(VENDOR, "SOURCE_FILES", (("LICENSE", "LICENSE"),)),
            patch.object(VENDOR, "verify", side_effect=RuntimeError("injected verification")),
            self.assertRaisesRegex(RuntimeError, "injected verification"),
        ):
            VENDOR.install_snapshot(ROOT, self.target, manifest)
        self.assertEqual("existing-target\n", destination.read_text())

    def test_verify_rejects_every_identity_and_manifest_boundary(self) -> None:
        commit = "d" * 40
        with patch("builtins.print"):
            VENDOR.sync(ROOT, self.target, CURRENT_VERSION, commit)
        original = json.loads((self.target / VENDOR.LOCK_NAME).read_text())
        variants = []
        value = json.loads(json.dumps(original))
        value["schema_version"] = 2
        variants.append(value)
        value = json.loads(json.dumps(original))
        value["upstream"] = {}
        variants.append(value)
        value = json.loads(json.dumps(original))
        value["upstream"]["repository"] = "other"
        variants.append(value)
        value = json.loads(json.dumps(original))
        value["upstream"]["version"] = "latest"
        variants.append(value)
        value = json.loads(json.dumps(original))
        value["files"] = {}
        variants.append(value)
        value = json.loads(json.dumps(original))
        first = next(iter(value["files"]))
        value["files"][first] = {}
        variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                (self.target / VENDOR.LOCK_NAME).write_text(json.dumps(value))
                VENDOR.verify(self.target)
        (self.target / VENDOR.LOCK_NAME).write_text(json.dumps(original))
        core = self.target / "tools/handoffctl.py"
        core.write_text(
            core.read_text().replace(
                f'COORDINATOR_VERSION = "{CURRENT_VERSION.removeprefix("v")}"',
                'COORDINATOR_VERSION = "9.9.9"',
            )
        )
        original["files"]["tools/handoffctl.py"]["sha256"] = VENDOR.sha256(core)
        (self.target / VENDOR.LOCK_NAME).write_text(json.dumps(original))
        with self.assertRaisesRegex(RuntimeError, "runtime version"):
            VENDOR.verify(self.target)
        missing = self.target / "missing"
        with self.assertRaisesRegex(RuntimeError, "regular file"):
            VENDOR.sha256(missing)
        with (
            patch.object(VENDOR, "git_output", side_effect=["", "short", CURRENT_VERSION]),
            self.assertRaisesRegex(RuntimeError, "full commit"),
        ):
            VENDOR.release_identity(ROOT, CURRENT_VERSION)

    def test_install_failure_rolls_back_every_destination(self) -> None:
        staged = self.target / "staged"
        destination_root = self.target / "installed"
        staged.mkdir()
        destination_root.mkdir()
        for name in ("one", "two"):
            (staged / name).write_text(f"new-{name}\n")
            (destination_root / name).write_text(f"old-{name}\n")
        original_replace = Path.replace

        def fail_second_install(path: Path, target: Path) -> Path:
            if path == staged / "two":
                raise OSError(5, "injected rename failure")
            return original_replace(path, target)

        with (
            patch.object(Path, "replace", fail_second_install),
            self.assertRaisesRegex(OSError, "injected rename"),
        ):
            VENDOR.install_staged_snapshot(staged, destination_root, ["one", "two"])
        self.assertEqual("old-one\n", (destination_root / "one").read_text())
        self.assertEqual("old-two\n", (destination_root / "two").read_text())

        (staged / "link").write_text("new\n")
        (destination_root / "link").symlink_to(destination_root / "one")
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            VENDOR.install_staged_snapshot(staged, destination_root, ["link"])

    def test_staging_failure_preserves_existing_vendor_snapshot(self) -> None:
        with patch("builtins.print"):
            VENDOR.sync(ROOT, self.target, CURRENT_VERSION, "a" * 40)
        before = {
            destination: (self.target / destination).read_bytes()
            for _, destination in VENDOR.SOURCE_FILES
        }
        before[VENDOR.LOCK_NAME] = (self.target / VENDOR.LOCK_NAME).read_bytes()
        original = VENDOR.atomic_bytes
        calls = 0

        def fail_during_staging(path: Path, content: bytes, mode: int = 0o644) -> None:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError(28, "No space left on device")
            original(path, content, mode)

        with (
            patch.object(VENDOR, "atomic_bytes", side_effect=fail_during_staging),
            self.assertRaisesRegex(OSError, "No space left"),
        ):
            VENDOR.sync(ROOT, self.target, CURRENT_VERSION, "b" * 40)
        after = {
            destination: (self.target / destination).read_bytes()
            for _, destination in VENDOR.SOURCE_FILES
        }
        after[VENDOR.LOCK_NAME] = (self.target / VENDOR.LOCK_NAME).read_bytes()
        self.assertEqual(before, after)

    def test_atomic_copy_rejects_symlink_and_git_query_is_bounded(self) -> None:
        source = self.target / "source"
        source.write_text("value")
        destination = self.target / "destination"
        destination.symlink_to(source)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            VENDOR.atomic_bytes(destination, b"replacement")
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "head\n", "")
            self.assertEqual("head", VENDOR.git_output(ROOT, "rev-parse", "HEAD"))
            self.assertEqual(30, run.call_args.kwargs["timeout"])

    def test_main_dispatches_sync_and_verify(self) -> None:
        with (
            patch.object(sys, "argv", ["vendor", "verify", "--target", str(self.target)]),
            patch.object(VENDOR, "verify") as verify,
        ):
            self.assertEqual(0, VENDOR.main())
            verify.assert_called_once_with(self.target)
        with (
            patch.object(
                sys,
                "argv",
                [
                    "vendor",
                    "sync",
                    "--source",
                    str(ROOT),
                    "--target",
                    str(self.target),
                    "--version",
                    CURRENT_VERSION,
                ],
            ),
            patch.object(VENDOR, "release_identity", return_value="c" * 40),
            patch.object(VENDOR, "sync") as sync,
        ):
            self.assertEqual(0, VENDOR.main())
        sync.assert_called_once_with(ROOT, self.target, CURRENT_VERSION, "c" * 40)
        with (
            patch.object(
                sys,
                "argv",
                [
                    "vendor",
                    "sync-development",
                    "--source",
                    str(ROOT),
                    "--target",
                    str(self.target),
                    "--commit",
                    "d" * 40,
                ],
            ),
            patch.object(VENDOR, "sync_development") as sync_development,
        ):
            self.assertEqual(0, VENDOR.main())
        sync_development.assert_called_once_with(ROOT, self.target, "d" * 40)


if __name__ == "__main__":
    unittest.main()
