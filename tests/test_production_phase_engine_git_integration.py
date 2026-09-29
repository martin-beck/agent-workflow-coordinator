# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
# ruff: noqa: S603, S607

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from tools.admission_lease import AdmissionLease, AdmissionRecheck
from tools.authority_neutral_commit import CommitAdmissionBundle
from tools.generate_upgrade_contract import generate
from tools.git_authority_adapter import GitAuthorityAdapter
from tools.handoffctl import locked
from tools.lock_domain_scope import LockDomainScope
from tools.mutation_fence import MutationFence, provision, provision_control_binding
from tools.production_phase_engine import (
    ForwardCommitCapabilityInputs,
    ForwardPhaseCapabilityInputs,
    build_production_phase_binding,
)
from tools.rollback_control_store import SQLiteBarrierSessionStore, SQLiteRollbackControlStore
from tools.runtime_bootstrap import (
    ExpectedRuntimeIdentity,
    VerifiedManifest,
    resolve_selected_runtime_bound,
)
from tools.upgrade_admission import PREFLIGHT_PREDICATES, QUIESCENCE_PREDICATES, REOPEN_PREDICATES
from tools.upgrade_binding import LiveUpgradeBinding, UpgradeRuntimeBinding
from tools.upgrade_identity import (
    BarrierChildIdentity,
    BarrierSessionIdentity,
    canonical_barrier_digest,
    canonical_barrier_session_digest,
    canonical_envelope_digest,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _contract() -> dict[str, Any]:
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
            "operation_id": "git-integration-upgrade",
            "backend": "git",
            "selector_ref": ".runtime/runtime-selector.json",
            "expected_state_revision": 3,
            "barrier_id": "git-integration-barrier",
            "fencing_token": "git-integration-fence",
            "from": release("v0.1.0", "a"),
            "to": release("v0.2.0", "b"),
        }
    )


class RealGitProductionPhaseEngineTests(unittest.TestCase):
    def test_real_git_factory_runs_all_eight_phases_and_selects_runtime(self) -> None:
        contract = _contract()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "authority"
            repo.mkdir(mode=0o700)
            artifacts = root / "artifacts"
            artifacts.mkdir(mode=0o700)
            _git(repo, "init", "-b", "main")
            (repo / ".git").chmod(0o700)
            _git(repo, "config", "user.name", "Integration Runner")
            _git(repo, "config", "user.email", "integration@example.invalid")
            runtime_selector = repo / ".runtime"
            runtime_selector.mkdir(mode=0o700)
            selector = runtime_selector / "runtime-selector.json"
            selector.write_text(
                '{"schema_version":1,"active_release":"v0.1.0","previous_release":"v0.0.9"}\n',
                encoding="utf-8",
            )
            (repo / "state").write_text("old\n", encoding="utf-8")
            _git(repo, "add", ".runtime/runtime-selector.json", "state")
            _git(repo, "commit", "-m", "initial authority")
            expected_head = _git(repo, "rev-parse", "HEAD")

            project = str(uuid.uuid4())
            authority = root / "authority.sqlite"
            with closing(sqlite3.connect(authority)) as connection:
                connection.execute("CREATE TABLE marker (value TEXT)")
                connection.commit()
            authority.chmod(0o600)
            control = SQLiteRollbackControlStore(root / "control.sqlite", project, authority)
            session = SQLiteBarrierSessionStore(control, lambda: "git-integration-authority")
            identity_record: dict[str, object] = {
                "schema_version": 1,
                "project_id": project,
                "attempt_id": "git-integration-attempt",
                "state_revision": 3,
                "authority_revision_at_acquire": "git-integration-authority",
                "durable_barrier_id": "git-integration-barrier",
                "fencing_token": "git-integration-fence",
                "fencing_owner": "git-integration-owner",
                "identity_digest": "0" * 64,
            }
            identity_record["identity_digest"] = canonical_barrier_session_digest(identity_record)
            identity = BarrierSessionIdentity.from_record(identity_record)
            session.create(identity)
            session.bind_child(
                1, BarrierChildIdentity.bind(identity, "git-integration-upgrade:commit", "new")
            )
            session.bind_child(
                2, BarrierChildIdentity.bind(identity, "git-integration-upgrade", "rollback")
            )

            marker = root / "authority-marker.json"
            lifecycle = root / "authority-lifecycle.json"
            authority_lock = root / "authority.lock"
            control_binding = root / "control-binding.json"
            provision(authority, marker, lifecycle, authority_lock, project)
            provision_control_binding(
                control.path, control_binding, control.control_lock_path, project
            )
            fence = MutationFence(
                authority,
                marker,
                lifecycle,
                authority_lock,
                control.path,
                control_binding,
                control.control_lock_path,
            )
            lease = AdmissionLease(
                project_id=project,
                authority_revision="git-integration-authority",
                fencing_token="git-integration-fence",  # noqa: S106
                fencing_owner="git-integration-owner",
                durable_barrier_id="git-integration-barrier",
                revision=3,
            )
            recheck = AdmissionRecheck(
                lease=lease,
                project_id=project,
                authority_revision="git-integration-authority",
                fencing_token="git-integration-fence",  # noqa: S106
                fencing_owner="git-integration-owner",
                durable_barrier_id="git-integration-barrier",
                revision=3,
            )
            scope = LockDomainScope.bind(session, fence, lease, recheck, locked)
            envelope: dict[str, object] = {
                "schema_version": 2,
                "backend": "git",
                "project_id": project,
                "operation_id": contract["operation_id"],
                "state_revision": 3,
                "authority_revision": "git-integration-authority",
                "fencing_token": "git-integration-fence",
                "fencing_owner": "git-integration-owner",
                "durable_barrier_id": "git-integration-barrier",
                "artifact_root": str(artifacts),
                "source": str(repo),
                "destination": str(artifacts / "backup.git"),
                "manifest": str(artifacts / "manifest.json"),
                "selector_ref": ".runtime/runtime-selector.json",
                "target": "rollback",
            }
            envelope["barrier_identity_digest"] = canonical_barrier_digest(envelope)
            envelope["envelope_digest"] = canonical_envelope_digest(envelope)
            runtime = UpgradeRuntimeBinding.bind(
                contract, envelope, session_identity_digest=identity.identity_digest
            )
            adapter = GitAuthorityAdapter(repo)
            live = LiveUpgradeBinding.bind(
                runtime,
                session.snapshot(),
                scope,
                lease,
                recheck,
                adapter,
                expected_git_repository=repo,
            )
            forward = dict(envelope, target="new")
            forward["barrier_identity_digest"] = canonical_barrier_digest(forward)
            forward["envelope_digest"] = canonical_envelope_digest(forward)

            releases = artifacts / "releases"
            releases.mkdir(mode=0o700)
            runtime_root = releases / "v0.2.0"
            runtime_root.mkdir(mode=0o700)
            manifest = runtime_root / "runtime-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "release": "v0.2.0",
                        "source_commit": "a" * 40,
                        "tag_ref": "refs/tags/v0.2.0",
                        "tag_object": "b" * 40,
                        "signature_sha256": "c" * 64,
                        "trust_policy_sha256": "d" * 64,
                        "vendor_manifest_sha256": "e" * 64,
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            manifest.chmod(0o600)
            manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            selector_admission = artifacts / "runtime-selector.json"
            selector_admission.write_text(
                '{"schema_version":1,"active_release":"v0.2.0","previous_release":"v0.1.0"}\n',
                encoding="utf-8",
            )
            selector_admission.chmod(0o600)
            expected_identity = ExpectedRuntimeIdentity(
                source_commit="a" * 40,
                tag_ref="refs/tags/v0.2.0",
                tag_object="b" * 40,
                signature_sha256="c" * 64,
                trust_policy_sha256="d" * 64,
                vendor_manifest_sha256="e" * 64,
            )
            resolved = resolve_selected_runtime_bound(
                selector_admission,
                releases,
                expected_identity,
                lambda _path, identity: VerifiedManifest("v0.2.0", identity, manifest_digest),
            )
            validation_admission = resolved.admit_for_dispatch()
            phase_evidence: dict[str, dict[str, object]] = {
                "discover": {
                    **forward,
                    "release_authentic": True,
                    "runtime_supported": True,
                    "backend_identity_verified": True,
                },
                "preflight": {
                    **forward,
                    **dict.fromkeys(PREFLIGHT_PREDICATES, True),
                    "capacity_verified": True,
                    "preflight_admitted": True,
                },
                "quiesce": {
                    **forward,
                    **dict.fromkeys(QUIESCENCE_PREDICATES, True),
                    "barrier_acquired": True,
                    "fencing_verified": True,
                    "barrier_status": "held",
                },
                "reopen": {
                    **forward,
                    **dict.fromkeys(REOPEN_PREDICATES, True),
                    "validated": True,
                    "barrier_held": True,
                    "reopen_barrier_status": "released",
                },
            }
            commit_admission = CommitAdmissionBundle(
                backend="git",
                target="new",
                operation_id="git-integration-upgrade:commit",
                fencing_token="git-integration-fence",  # noqa: S106
                state_revision=3,
                barrier_id="git-integration-barrier",
                artifact_identity="artifact",
                manifest_identity="manifest",
                selector_identity="selector",
                runtime_identity="runtime",
            )
            commit_snapshot = {
                **forward,
                **dict.fromkeys(QUIESCENCE_PREDICATES, True),
                "barrier_status": "held",
            }

            def reread() -> Mapping[str, object]:
                state = session.snapshot()
                assert state is not None
                return {
                    "backend": "git",
                    "target": "new",
                    "operation_id": "git-integration-upgrade:commit",
                    "state_revision": state.revision,
                    "barrier_id": "git-integration-barrier",
                    "fencing_token": "git-integration-fence",
                    "artifact_identity": "artifact",
                    "manifest_identity": "manifest",
                    "selector_identity": "selector",
                    "runtime_identity": "runtime",
                }

            inputs = ForwardPhaseCapabilityInputs(
                engine_context=forward,
                backup_context=dict(forward, binding={"project_id": project}),
                stage_context=dict(
                    forward,
                    runtime_root=str(runtime_root),
                    manifest_digest=manifest_digest,
                    binding={"release": "v0.2.0", "manifest_digest": manifest_digest},
                ),
                validation_admission=validation_admission,
                validation_context=dict(
                    forward,
                    binding={"release": "v0.2.0", "manifest_digest": manifest_digest},
                ),
                commit=ForwardCommitCapabilityInputs(
                    admission=commit_admission,
                    session_revision=3,
                    admission_reread=reread,
                    argument={
                        "message": "select staged runtime",
                        "path": ".runtime/runtime-selector.json",
                        "content": '{"schema_version":1,"active_release":"v0.2.0",'
                        '"previous_release":"v0.1.0"}\n',
                    },
                    evidence={
                        "quiesced": True,
                        "backup_verified": True,
                        "selector_verified": True,
                        "selector_commit_atomic": True,
                        "fencing_verified": True,
                        "selector_before_verified": True,
                        "selector_after_verified": True,
                        "admitted_snapshot": commit_snapshot,
                        "current_snapshot": commit_snapshot,
                    },
                    effect_journal=session,
                ),
                readiness_evidence=phase_evidence,
            )
            try:
                result = build_production_phase_binding(
                    contract, live, root / "journal.json", forward_inputs=inputs
                )
                result.engine.plan()
                applied = result.engine.apply(
                    {
                        phase: (lambda _step, _state: {})
                        for phase in (
                            "discover",
                            "preflight",
                            "quiesce",
                            "backup",
                            "stage",
                            "commit",
                            "validate",
                            "reopen",
                        )
                    }
                )
            finally:
                validation_admission.close()
            self.assertEqual("completed", applied["status"])
            self.assertIn('"active_release":"v0.2.0"', selector.read_text(encoding="utf-8"))
            self.assertNotEqual(expected_head, _git(repo, "rev-parse", "HEAD"))
            self.assertEqual("", _git(repo, "status", "--porcelain=v1", "--untracked-files=all"))


if __name__ == "__main__":
    unittest.main()
