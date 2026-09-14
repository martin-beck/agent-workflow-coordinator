# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile tests for the uncalled concrete lock-domain scope."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.admission_lease import AdmissionLease
from tools.handoffctl import locked
from tools.lock_domain import LockDomainContract, LockDomainError
from tools.lock_domain_scope import LockDomainScope
from tools.mutation_fence import MutationFence, provision, provision_control_binding
from tools.rollback_control_store import (
    SQLiteBarrierSessionStore,
    SQLiteRollbackControlStore,
)
from tools.upgrade_identity import BarrierSessionIdentity, canonical_barrier_session_digest

PROJECT = "11111111-1111-4111-8111-111111111111"


def identity() -> BarrierSessionIdentity:
    record: dict[str, object] = {
        "schema_version": 1,
        "project_id": PROJECT,
        "attempt_id": "attempt-1",
        "state_revision": 1,
        "authority_revision_at_acquire": "authority-1",
        "durable_barrier_id": "barrier-1",
        "fencing_token": "fence-1",
        "fencing_owner": "owner-1",
        "identity_digest": "0" * 64,
    }
    record["identity_digest"] = canonical_barrier_session_digest(record)
    return BarrierSessionIdentity.from_record(record)


class LockDomainScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.root.chmod(0o700)
        self.authority = self.root / "authority.sqlite"
        self.authority.write_bytes(b"authority")
        self.authority.chmod(0o600)
        control = self.root / "control.sqlite"
        self.store = SQLiteRollbackControlStore(control, PROJECT, self.authority)
        marker = self.root / "authority-marker.json"
        lifecycle = self.root / "authority-lifecycle.json"
        authority_lock = self.root / "authority.lock"
        binding = self.root / "control-binding.json"
        provision(self.authority, marker, lifecycle, authority_lock, PROJECT)
        provision_control_binding(control, binding, self.store.control_lock_path, PROJECT)
        self.fence = MutationFence(
            self.authority,
            marker,
            lifecycle,
            authority_lock,
            control,
            binding,
            self.store.control_lock_path,
        )
        self.session = SQLiteBarrierSessionStore(self.store, lambda: "authority-1")
        self.session.create(identity())
        with locked() as guard:
            self.domain = LockDomainContract.capture(guard, self.session, self.fence)
        self.lease = AdmissionLease(PROJECT, "authority-1", "fence-1", "owner-1", "barrier-1", 1)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_scope_proves_durable_session_inside_all_three_locks(self) -> None:
        scope = LockDomainScope(self.domain, self.session, self.fence, self.lease, locked)
        events: list[str] = []
        with scope.hold():
            events.append("held")
            self.assertTrue(self.session.operation_owned_by_current_thread)
        self.assertEqual(["held"], events)
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_scope_rejects_lease_drift_and_releases_control(self) -> None:
        for drifted in (
            AdmissionLease(PROJECT, "authority-1", "other", "owner-1", "barrier-1", 1),
            AdmissionLease(PROJECT, "authority-1", "fence-1", "owner-1", "barrier-1", 2),
        ):
            scope = LockDomainScope(self.domain, self.session, self.fence, drifted, locked)
            with (
                self.subTest(drifted=drifted),
                self.assertRaisesRegex(LockDomainError, "do not match"),
                scope.hold(),
            ):
                self.fail("unreachable")
            self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_scope_rejects_replaced_control_descriptor_before_authority(self) -> None:
        replacement = self.root / "replacement.lock"
        replacement.write_bytes(b"")
        replacement.chmod(0o600)
        self.store.control_lock_path.unlink()
        replacement.replace(self.store.control_lock_path)
        scope = LockDomainScope(self.domain, self.session, self.fence, self.lease, locked)
        with self.assertRaisesRegex(LockDomainError, "binding|identity"), scope.hold():
            self.fail("unreachable")


if __name__ == "__main__":
    unittest.main()
