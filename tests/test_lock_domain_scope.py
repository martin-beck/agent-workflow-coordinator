# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile tests for the uncalled concrete lock-domain scope."""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

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


def _scope_process(
    root_text: str, start: Any, starts: Any, ends: Any, index: int, crash: bool
) -> None:
    root = Path(root_text)
    authority = root / "authority.sqlite"
    control = root / "control.sqlite"
    store = SQLiteRollbackControlStore(control, PROJECT, authority)
    session = SQLiteBarrierSessionStore(store, lambda: "authority-1")
    fence = MutationFence(
        authority,
        root / "authority-marker.json",
        root / "authority-lifecycle.json",
        root / "authority.lock",
        control,
        root / "control-binding.json",
        store.control_lock_path,
    )
    with locked() as guard:
        domain = LockDomainContract.capture(guard, session, fence)
    lease = AdmissionLease(PROJECT, "authority-1", "fence-1", "owner-1", "barrier-1", 1)
    scope = LockDomainScope(domain, session, fence, lease, locked)
    start.wait()
    with scope.hold():
        starts[index] = time.monotonic_ns()
        if crash:
            os._exit(17)
        time.sleep(0.05)
        ends[index] = time.monotonic_ns()


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

    def test_abort_then_recheck_rejects_replaced_authority(self) -> None:
        """A caller abort cannot make a replaced authority descriptor admissible."""
        scope = LockDomainScope(self.domain, self.session, self.fence, self.lease, locked)
        with self.assertRaisesRegex(SystemExit, "simulated abort"), scope.hold():
            raise SystemExit("simulated abort")
        self.assertFalse(self.session.operation_owned_by_current_thread)

        replacement = self.root / "replacement-authority.sqlite"
        replacement.write_bytes(b"replacement")
        replacement.chmod(0o600)
        self.authority.unlink()
        replacement.replace(self.authority)
        with self.assertRaisesRegex(LockDomainError, "binding|identity"), scope.hold():
            self.fail("replaced authority must be rejected after abort")
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_abort_then_recheck_rejects_durable_session_state_change(self) -> None:
        """A durable ambiguous session remains fail-closed after caller abort."""
        scope = LockDomainScope(self.domain, self.session, self.fence, self.lease, locked)
        with self.assertRaisesRegex(SystemExit, "simulated abort"), scope.hold():
            raise SystemExit("simulated abort")
        self.assertFalse(self.session.operation_owned_by_current_thread)

        self.session.mark_ambiguous(1, "caller-abort")
        with self.assertRaisesRegex(LockDomainError, "not held"), scope.hold():
            self.fail("ambiguous durable session must be rejected")
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_abort_then_recheck_rejects_replaced_session_identity(self) -> None:
        """A replaced durable authority revision cannot pass the old lease."""
        scope = LockDomainScope(self.domain, self.session, self.fence, self.lease, locked)
        with self.assertRaisesRegex(SystemExit, "simulated crash"), scope.hold():
            raise SystemExit("simulated crash")
        self.assertFalse(self.session.operation_owned_by_current_thread)

        changed_record = replace(
            identity(), authority_revision_at_acquire="authority-2", state_revision=2
        ).as_record()
        changed_record["identity_digest"] = canonical_barrier_session_digest(changed_record)
        with sqlite3.connect(self.store.control_store_path) as connection:
            connection.execute(
                "UPDATE barrier_session SET authority_revision_at_acquire=?, "
                "state_revision=?, identity_digest=?, revision=? WHERE project_id=?",
                (
                    changed_record["authority_revision_at_acquire"],
                    changed_record["state_revision"],
                    changed_record["identity_digest"],
                    2,
                    PROJECT,
                ),
            )
            connection.commit()

        with self.assertRaisesRegex(LockDomainError, "do not match"), scope.hold():
            self.fail("replaced durable session identity must be rejected")
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_two_process_scopes_never_overlap(self) -> None:
        context = multiprocessing.get_context("fork")
        start = context.Event()
        starts = context.Array("q", [0, 0], lock=False)
        ends = context.Array("q", [0, 0], lock=False)
        workers = [
            context.Process(
                target=_scope_process,
                args=(self.directory.name, start, starts, ends, index, False),
            )
            for index in (0, 1)
        ]
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(5)
            self.assertEqual(0, worker.exitcode)
        self.assertTrue(
            ends[0] <= starts[1] or ends[1] <= starts[0],
            (list(starts), list(ends)),
        )

    def test_process_death_releases_scope_for_fresh_recheck(self) -> None:
        context = multiprocessing.get_context("fork")
        start = context.Event()
        starts = context.Array("q", [0], lock=False)
        ends = context.Array("q", [0], lock=False)
        crashed = context.Process(
            target=_scope_process,
            args=(self.directory.name, start, starts, ends, 0, True),
        )
        crashed.start()
        start.set()
        crashed.join(5)
        self.assertEqual(17, crashed.exitcode)

        start = context.Event()
        recovered = context.Process(
            target=_scope_process,
            args=(self.directory.name, start, starts, ends, 0, False),
        )
        recovered.start()
        start.set()
        recovered.join(5)
        self.assertEqual(0, recovered.exitcode)
        self.assertGreater(ends[0], starts[0])


if __name__ == "__main__":
    unittest.main()
