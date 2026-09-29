# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, cast

from tools.generate_upgrade_contract import generate
from tools.handoffctl import locked
from tools.mutation_fence import provision, provision_control_binding
from tools.production_upgrade_binding import ProductionBindingError, resolve_sqlite_live_binding
from tools.rollback_control_store import SQLiteBarrierSessionStore, SQLiteRollbackControlStore
from tools.upgrade_authority import inspect_sqlite_release_authority
from tools.upgrade_binding import LiveUpgradeBinding, UpgradeRuntimeBinding
from tools.upgrade_identity import (
    BarrierChildIdentity,
    BarrierSessionIdentity,
    canonical_barrier_digest,
    canonical_barrier_session_digest,
    canonical_envelope_digest,
)


def _runtime(backend: str) -> tuple[dict[str, object], dict[str, object]]:
    operation = "production-binding-test"
    contract = generate(
        {
            "operation_id": operation,
            "backend": backend,
            "selector_ref": ".runtime/runtime-selector.json",
            "expected_state_revision": 1,
            "barrier_id": "barrier-test",
            "fencing_token": "fence-test",
            "from": {
                "version": "v0.3.5",
                "source_commit": "a" * 40,
                "tag_ref": "refs/tags/v0.3.5",
                "tag_object": "b" * 40,
                "signature_sha256": "c" * 64,
                "trust_policy_sha256": "d" * 64,
                "vendor_manifest_sha256": "e" * 64,
            },
            "to": {
                "version": "v0.3.6",
                "source_commit": "f" * 40,
                "tag_ref": "refs/tags/v0.3.6",
                "tag_object": "0" * 40,
                "signature_sha256": "1" * 64,
                "trust_policy_sha256": "2" * 64,
                "vendor_manifest_sha256": "3" * 64,
            },
        }
    )
    root = PurePosixPath("/srv/data/projects/production-binding-test")
    envelope: dict[str, object] = {
        "schema_version": 2,
        "backend": backend,
        "project_id": str(uuid.uuid4()),
        "operation_id": operation,
        "state_revision": 1,
        "authority_revision": "authority-test",
        "fencing_token": "fence-test",
        "fencing_owner": "test-owner",
        "durable_barrier_id": "barrier-test",
        "artifact_root": str(root),
        "source": str(root / "source"),
        "destination": str(root / "destination"),
        "manifest": str(root / "manifest.json"),
        "selector_ref": ".runtime/runtime-selector.json",
        "target": "rollback",
    }
    envelope["barrier_identity_digest"] = canonical_barrier_digest(envelope)
    envelope["envelope_digest"] = canonical_envelope_digest(envelope)
    return contract, envelope


class ProductionUpgradeBindingTests(unittest.TestCase):
    def test_resolver_reconstructs_real_durable_sqlite_scope(self) -> None:
        contract, envelope = _runtime("sqlite")
        project_id = str(envelope["project_id"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            runtime_root = root / ".runtime"
            runtime_root.mkdir(mode=0o700)
            authority = root / "authority.sqlite"
            with sqlite3.connect(authority) as connection:
                connection.executescript(
                    """
                    CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE tasks(id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL,
                        meta_json TEXT NOT NULL, body TEXT NOT NULL, revision INTEGER NOT NULL,
                        status TEXT NOT NULL, owner TEXT NOT NULL, claim_expires TEXT NOT NULL,
                        branch TEXT NOT NULL, worktree_key TEXT NOT NULL, updated_at TEXT NOT NULL);
                    CREATE TABLE dependencies(task_id TEXT NOT NULL, dependency_id TEXT NOT NULL);
                    CREATE TABLE events(sequence INTEGER PRIMARY KEY, task_id TEXT NOT NULL,
                        revision INTEGER NOT NULL, kind TEXT NOT NULL, recorded_at TEXT NOT NULL,
                        note TEXT NOT NULL);
                    CREATE TABLE command_results(
                        sequence INTEGER PRIMARY KEY, task_id TEXT NOT NULL,
                        owner TEXT NOT NULL, argv_sha256 TEXT NOT NULL, returncode INTEGER NOT NULL,
                        classification TEXT NOT NULL, recorded_at TEXT NOT NULL);
                    CREATE TABLE migrations(
                        sequence INTEGER PRIMARY KEY, source_backend TEXT NOT NULL,
                        source_checkpoint TEXT NOT NULL, imported_at TEXT NOT NULL,
                        finalized INTEGER NOT NULL);
                    CREATE TABLE checkpoints(name TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                        recorded_at TEXT NOT NULL);
                    INSERT INTO metadata(key, value) VALUES
                        ('schema_version', '1'), ('backend', 'sqlite'),
                        ('project_id', 'placeholder'),
                        ('state_repository', 'owner/state'),
                        ('product_repository', 'owner/product'),
                        ('state', 'active');
                    """
                )
                connection.execute(
                    "UPDATE metadata SET value = ? WHERE key = 'project_id'", (project_id,)
                )
                connection.commit()
            authority.chmod(0o600)
            control_path = runtime_root / "coordinator.control.sqlite3"
            marker = runtime_root / "coordinator.authority-marker.json"
            lifecycle = runtime_root / "coordinator.authority-lifecycle.json"
            authority_lock = runtime_root / "coordinator.authority.lock"
            control_binding = runtime_root / "coordinator.control-binding.json"
            provision(authority, marker, lifecycle, authority_lock, project_id)
            control = SQLiteRollbackControlStore(control_path, project_id, authority)
            provision_control_binding(
                control_path, control_binding, control.control_lock_path, project_id
            )
            project_binding_path = root / "coordinator.binding.json"
            project_binding_path.write_text(
                f'{{"schema_version":1,"project_id":"{project_id}","state_repository":"owner/state","product_repository":"owner/product"}}\n',
                encoding="utf-8",
            )
            backend_config = root / "coordinator.backend.json"
            backend_config.write_text(
                f'{{"schema_version":1,"project_id":"{project_id}","backend":"sqlite"}}\n',
                encoding="utf-8",
            )
            selector = runtime_root / "runtime-selector.json"
            selector.write_text(
                '{"active_release":"v0.3.6","previous_release":"v0.3.5","schema_version":1}\n',
                encoding="utf-8",
            )
            authority_revision = inspect_sqlite_release_authority(
                authority,
                project_binding_path,
                backend_config,
                selector,
                project_id,
                "v0.3.6",
                "v0.3.5",
            ).authority_revision
            envelope["authority_revision"] = authority_revision
            envelope["barrier_identity_digest"] = canonical_barrier_digest(envelope)
            envelope["envelope_digest"] = canonical_envelope_digest(envelope)
            session_record: dict[str, object] = {
                "schema_version": 1,
                "project_id": project_id,
                "attempt_id": "production-binding-attempt",
                "state_revision": 1,
                "authority_revision_at_acquire": authority_revision,
                "durable_barrier_id": str(envelope["durable_barrier_id"]),
                "fencing_token": str(envelope["fencing_token"]),
                "fencing_owner": str(envelope["fencing_owner"]),
            }
            session_record["identity_digest"] = canonical_barrier_session_digest(session_record)
            identity = BarrierSessionIdentity.from_record(session_record)
            session = SQLiteBarrierSessionStore(control, lambda: authority_revision)
            session.create(identity)
            forward = BarrierChildIdentity.bind(
                identity, f"{envelope['operation_id']}:forward", "new"
            )
            child = BarrierChildIdentity.bind(identity, str(envelope["operation_id"]), "rollback")
            session.bind_child(1, forward)
            session.bind_child(2, child)
            binding = UpgradeRuntimeBinding.bind(
                contract, envelope, session_identity_digest=identity.identity_digest
            )
            resolved = resolve_sqlite_live_binding(
                binding,
                database=authority,
                control_database=control_path,
                authority_marker=marker,
                authority_lifecycle=lifecycle,
                authority_lock=authority_lock,
                control_binding=control_binding,
                control_lock=control.control_lock_path,
                project_binding=project_binding_path,
                backend_config=backend_config,
                runtime_selector=selector,
                common_lock=locked,
            )
            self.assertIsInstance(resolved, LiveUpgradeBinding)
            lease = cast(Any, resolved.lease)
            runtime = cast(Any, resolved.runtime)
            self.assertEqual(identity.state_revision, lease.revision)
            self.assertEqual(identity.identity_digest, runtime.session_identity_digest)
            self.assertEqual(authority_revision, lease.authority_revision)

    def test_git_resolution_is_explicitly_fail_closed(self) -> None:
        contract, envelope = _runtime("git")
        runtime = UpgradeRuntimeBinding.bind(contract, envelope, session_identity_digest="a" * 64)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ProductionBindingError, "canonical SQLite"):
                resolve_sqlite_live_binding(
                    runtime,
                    database=root / "authority.sqlite",
                    control_database=root / "control.sqlite",
                    authority_marker=root / "marker.json",
                    authority_lifecycle=root / "lifecycle.json",
                    authority_lock=root / "authority.lock",
                    control_binding=root / "control-binding.json",
                    control_lock=root / "control.lock",
                    project_binding=root / "coordinator.binding.json",
                    backend_config=root / "coordinator.backend.json",
                    runtime_selector=root / "runtime-selector.json",
                    common_lock=cast(Any, lambda: None),
                )

    def test_missing_sqlite_state_is_rejected_before_scope_construction(self) -> None:
        contract, envelope = _runtime("sqlite")
        runtime = UpgradeRuntimeBinding.bind(contract, envelope, session_identity_digest="a" * 64)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ProductionBindingError):
                resolve_sqlite_live_binding(
                    runtime,
                    database=root / "authority.sqlite",
                    control_database=root / "control.sqlite",
                    authority_marker=root / "marker.json",
                    authority_lifecycle=root / "lifecycle.json",
                    authority_lock=root / "authority.lock",
                    control_binding=root / "control-binding.json",
                    control_lock=root / "control.lock",
                    project_binding=root / "coordinator.binding.json",
                    backend_config=root / "coordinator.backend.json",
                    runtime_selector=root / "runtime-selector.json",
                    common_lock=cast(Any, lambda: None),
                )
