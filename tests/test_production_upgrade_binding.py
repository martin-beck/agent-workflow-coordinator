# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path, PurePosixPath

from tools.generate_upgrade_contract import generate
from tools.production_upgrade_binding import ProductionBindingError, resolve_sqlite_live_binding
from tools.upgrade_binding import UpgradeRuntimeBinding
from tools.upgrade_identity import canonical_barrier_digest, canonical_envelope_digest


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
                    common_lock=lambda: None,
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
                    common_lock=lambda: None,
                )
