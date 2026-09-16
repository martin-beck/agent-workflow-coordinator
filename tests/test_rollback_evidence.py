# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

import json
import tempfile
import unittest
from pathlib import Path

from tools.git_authority_adapter import GitAuthorityAdapter
from tools.rollback_control_store import SQLiteControlStoreAdapter, SQLiteRollbackControlStore
from tools.rollback_evidence import RollbackEvidenceError
from tools.sqlite_authority_adapter import SQLiteAuthorityAdapter

PROJECT = "11111111-1111-4111-8111-111111111111"


class RollbackEvidenceTests(unittest.TestCase):
    def test_initialized_sqlite_control_store_binds_observation_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = root / "authority.sqlite"
            authority.write_bytes(b"SQLite format 3\x00")
            authority.chmod(0o600)
            control = SQLiteRollbackControlStore(root / "control.sqlite", PROJECT, authority)
            context = {
                "schema_version": 2,
                "backend": "sqlite",
                "project_id": PROJECT,
                "operation_id": "op-1",
                "state_revision": 1,
                "authority_revision": "authority-1",
                "fencing_token": "fence-1",
                "fencing_owner": "owner-1",
                "durable_barrier_id": "barrier-1",
                "artifact_root": str(root),
                "source": str(authority),
                "destination": str(root / "backup.bin"),
                "manifest": str(root / "manifest.json"),
                "barrier_identity_digest": "0" * 64,
                "target": "rollback",
                "envelope_digest": "0" * 64,
            }
            from tools.upgrade_identity import canonical_barrier_digest, canonical_envelope_digest

            context["barrier_identity_digest"] = canonical_barrier_digest(context)
            context["envelope_digest"] = canonical_envelope_digest(context)
            record = {**context, "status": "held", "revision": 1}
            control.cas(0, record)
            backup = root / "backup.bin"
            manifest = root / "manifest.json"
            backup.write_bytes(b"backup")
            manifest.write_text(json.dumps({"version": 1}), encoding="utf-8")
            adapter = SQLiteControlStoreAdapter(
                SQLiteAuthorityAdapter(authority), control, object()
            )
            observation = adapter.observe_backup_identity(backup, manifest, context)
            stat = control.control_store_path.stat()
            self.assertEqual(f"{stat.st_dev}:{stat.st_ino}", observation.control_store_identity)
            self.assertEqual(1, observation.control_store_revision)

    def test_initialized_adapters_observe_identical_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            backup = root / "backup.bin"
            manifest = root / "manifest.json"
            backup.write_bytes(b"immutable-backup")
            manifest.write_text(json.dumps({"version": 1}), encoding="utf-8")
            git = GitAuthorityAdapter(root)
            database = root / "authority.sqlite"
            database.write_bytes(b"SQLite format 3\x00")
            database.chmod(0o600)
            sqlite = SQLiteAuthorityAdapter(database)
            git_observation = git.observe_backup_identity(
                backup, manifest, control_store_identity="store-1", control_store_revision=1
            )
            sqlite_observation = sqlite.observe_backup_identity(
                backup, manifest, control_store_identity="store-1", control_store_revision=1
            )
            self.assertEqual(git_observation, sqlite_observation)
            backup.write_bytes(b"replaced-backup")
            self.assertNotEqual(
                git_observation.backup_bytes_digest,
                git.observe_backup_identity(
                    backup, manifest, control_store_identity="store-1", control_store_revision=1
                ).backup_bytes_digest,
            )
            manifest.write_text(json.dumps({"version": 2}), encoding="utf-8")
            self.assertNotEqual(
                git_observation.manifest_digest,
                sqlite.observe_backup_identity(
                    backup, manifest, control_store_identity="store-1", control_store_revision=1
                ).manifest_digest,
            )

    def test_observation_rejects_tampered_manifest_and_cas_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = root / "backup.bin"
            manifest = root / "manifest.json"
            backup.write_bytes(b"backup")
            manifest.write_text("[]", encoding="utf-8")
            with self.assertRaises(RollbackEvidenceError):
                GitAuthorityAdapter.observe_backup_identity(
                    backup, manifest, control_store_identity="store", control_store_revision=1
                )
            manifest.write_text("{}", encoding="utf-8")
            with self.assertRaises(RollbackEvidenceError):
                GitAuthorityAdapter.observe_backup_identity(
                    backup, manifest, control_store_identity="", control_store_revision=1
                )
            with self.assertRaises(RollbackEvidenceError):
                GitAuthorityAdapter.observe_backup_identity(
                    backup, manifest, control_store_identity="store", control_store_revision=True
                )
