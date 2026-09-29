from __future__ import annotations

import hashlib
import json
import sqlite3
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
from tools.handoffctl import locked
from tools.lock_domain_scope import LockDomainScope
from tools.mutation_fence import MutationFence, provision, provision_control_binding
from tools.production_phase_engine import (
    ForwardCommitCapabilityInputs,
    ForwardPhaseCapabilityInputs,
    build_production_phase_binding,
)
from tools.rollback_control_store import (
    SQLiteBarrierSessionStore,
    SQLiteRollbackControlStore,
)
from tools.runtime_bootstrap import (
    DispatchAdmission,
    ExpectedRuntimeIdentity,
    VerifiedManifest,
    resolve_selected_runtime_bound,
)
from tools.sqlite_authority_adapter import SQLiteAuthorityAdapter
from tools.upgrade_admission import PREFLIGHT_PREDICATES, QUIESCENCE_PREDICATES, REOPEN_PREDICATES
from tools.upgrade_binding import LiveUpgradeBinding, UpgradeRuntimeBinding
from tools.upgrade_identity import (
    BarrierChildIdentity,
    BarrierSessionIdentity,
    canonical_barrier_digest,
    canonical_barrier_session_digest,
    canonical_envelope_digest,
)


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
            "operation_id": "integration-upgrade",
            "backend": "sqlite",
            "selector_ref": ".runtime/runtime-selector.json",
            "expected_state_revision": 3,
            "barrier_id": "integration-barrier",
            "fencing_token": "integration-fence",
            "from": release("v0.1.0", "a"),
            "to": release("v0.2.0", "b"),
        }
    )


class RealProductionPhaseEngineTests(unittest.TestCase):
    def test_real_sqlite_factory_runs_all_eight_phases(self) -> None:
        contract = _contract()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = root / "artifacts"
            artifacts.mkdir(mode=0o700)
            authority = root / "authority.sqlite"
            project = str(uuid.uuid4())
            with closing(sqlite3.connect(authority)) as connection, connection:
                connection.execute(
                    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.executemany(
                    "INSERT INTO metadata(key, value) VALUES (?, ?)",
                    (
                        ("schema_version", "1"),
                        ("backend", "sqlite"),
                        ("project_id", project),
                        ("state_repository", "state-repository"),
                        ("product_repository", "product-repository"),
                        ("state", "active"),
                    ),
                )
                connection.execute("CREATE TABLE state (id INTEGER PRIMARY KEY, value TEXT)")
                connection.execute("INSERT INTO state VALUES (1, 'old')")
            authority.chmod(0o600)

            envelope: dict[str, object] = {
                "schema_version": 2,
                "backend": "sqlite",
                "project_id": project,
                "operation_id": contract["operation_id"],
                "state_revision": 3,
                "authority_revision": "integration-authority",
                "fencing_token": "integration-fence",
                "fencing_owner": "integration-owner",
                "durable_barrier_id": "integration-barrier",
                "artifact_root": str(artifacts),
                "source": str(root / "source"),
                "destination": str(artifacts / "backup.sqlite"),
                "manifest": str(artifacts / "manifest.json"),
                "selector_ref": ".runtime/runtime-selector.json",
                "target": "rollback",
            }
            envelope["barrier_identity_digest"] = canonical_barrier_digest(envelope)
            envelope["envelope_digest"] = canonical_envelope_digest(envelope)

            control = SQLiteRollbackControlStore(root / "control.sqlite", project, authority)
            session = SQLiteBarrierSessionStore(control, lambda: "integration-authority")
            identity_record: dict[str, object] = {
                "schema_version": 1,
                "project_id": project,
                "attempt_id": "integration-attempt",
                "state_revision": 3,
                "authority_revision_at_acquire": "integration-authority",
                "durable_barrier_id": "integration-barrier",
                "fencing_token": "integration-fence",
                "fencing_owner": "integration-owner",
                "identity_digest": "0" * 64,
            }
            identity_record["identity_digest"] = canonical_barrier_session_digest(identity_record)
            identity = BarrierSessionIdentity.from_record(identity_record)
            session.create(identity)
            session.bind_child(
                1, BarrierChildIdentity.bind(identity, "integration-upgrade:commit", "new")
            )
            session.bind_child(
                2, BarrierChildIdentity.bind(identity, "integration-upgrade", "rollback")
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
                authority_revision="integration-authority",
                fencing_token="integration-fence",  # noqa: S106
                fencing_owner="integration-owner",
                durable_barrier_id="integration-barrier",
                revision=3,
            )
            recheck = AdmissionRecheck(
                lease=lease,
                project_id=project,
                authority_revision="integration-authority",
                fencing_token="integration-fence",  # noqa: S106
                fencing_owner="integration-owner",
                durable_barrier_id="integration-barrier",
                revision=3,
            )
            scope = LockDomainScope.bind(session, fence, lease, recheck, locked)
            runtime = UpgradeRuntimeBinding.bind(
                contract, envelope, session_identity_digest=identity.identity_digest
            )
            adapter = SQLiteAuthorityAdapter(authority)
            live = LiveUpgradeBinding.bind(
                runtime, session.snapshot(), scope, lease, recheck, adapter
            )

            forward = dict(envelope, target="new")
            forward["barrier_identity_digest"] = canonical_barrier_digest(forward)
            forward["envelope_digest"] = canonical_envelope_digest(forward)
            binding = {
                "project_id": project,
                "state_repository": "state-repository",
                "product_repository": "product-repository",
            }
            releases_root = artifacts / "releases"
            releases_root.mkdir(mode=0o700)
            runtime_root = releases_root / "v0.2.0"
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
            selector = artifacts / "runtime-selector.json"
            selector.write_text(
                '{"schema_version":1,"active_release":"v0.2.0","previous_release":"v0.1.0"}\n',
                encoding="utf-8",
            )
            selector.chmod(0o600)
            expected_identity = ExpectedRuntimeIdentity(
                source_commit="a" * 40,
                tag_ref="refs/tags/v0.2.0",
                tag_object="b" * 40,
                signature_sha256="c" * 64,
                trust_policy_sha256="d" * 64,
                vendor_manifest_sha256="e" * 64,
            )

            def admission() -> DispatchAdmission:
                resolved = resolve_selected_runtime_bound(
                    selector,
                    releases_root,
                    expected_identity,
                    lambda _path, identity: VerifiedManifest("v0.2.0", identity, manifest_digest),
                )
                return resolved.admit_for_dispatch()

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
                backend="sqlite",
                target="new",
                operation_id="integration-upgrade:commit",
                fencing_token="integration-fence",  # noqa: S106
                state_revision=3,
                barrier_id="integration-barrier",
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
            commit_evidence = {
                "quiesced": True,
                "backup_verified": True,
                "selector_verified": True,
                "selector_commit_atomic": True,
                "fencing_verified": True,
                "selector_before_verified": True,
                "selector_after_verified": True,
                "admitted_snapshot": commit_snapshot,
                "current_snapshot": commit_snapshot,
            }

            def reread() -> Mapping[str, object]:
                return {
                    "backend": "sqlite",
                    "target": "new",
                    "operation_id": "integration-upgrade:commit",
                    "state_revision": 3,
                    "barrier_id": "integration-barrier",
                    "fencing_token": "integration-fence",
                    "artifact_identity": "artifact",
                    "manifest_identity": "manifest",
                    "selector_identity": "selector",
                    "runtime_identity": "runtime",
                }

            def update(connection: sqlite3.Connection) -> None:
                connection.execute("UPDATE state SET value='new' WHERE id=1")

            stage_context = dict(
                forward,
                runtime_root=str(runtime_root),
                manifest_digest=manifest_digest,
                binding={"release": "v0.2.0", "manifest_digest": manifest_digest},
            )
            validation_context = dict(
                forward,
                binding={"release": "v0.2.0", "manifest_digest": manifest_digest},
            )
            validation_admission = admission()
            inputs = ForwardPhaseCapabilityInputs(
                engine_context=forward,
                backup_context=dict(forward, binding=binding),
                stage_context=stage_context,
                validation_admission=validation_admission,
                validation_context=validation_context,
                commit=ForwardCommitCapabilityInputs(
                    admission=commit_admission,
                    session_revision=3,
                    admission_reread=reread,
                    argument=update,
                    evidence=commit_evidence,
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

            self.assertEqual(applied["status"], "completed")
            with closing(sqlite3.connect(authority)) as connection:
                self.assertEqual(("new",), connection.execute("SELECT value FROM state").fetchone())


if __name__ == "__main__":
    unittest.main()
