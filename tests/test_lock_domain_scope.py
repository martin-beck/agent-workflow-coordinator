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
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from tools.admission_lease import (
    AdmissionLease,
    AdmissionLeaseError,
    AdmissionRecheck,
    validate_recheck,
)
from tools.admitted_control_store import AdmittedControlBinding
from tools.handoffctl import locked
from tools.lifecycle_trace import (
    LifecycleEvent,
    _issue_event,
    validate_model_action_contract,
    validate_model_trace,
    validate_terminal_recovery_contract,
)
from tools.lock_domain import LockDomainContract, LockDomainError
from tools.lock_domain_scope import LockDomainScope
from tools.mutation_fence import MutationFence, provision, provision_control_binding
from tools.rollback_control_store import (
    ControlStoreError,
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


def lease_recheck(lease: AdmissionLease) -> AdmissionRecheck:
    return validate_recheck(
        lease,
        project_id=lease.project_id,
        authority_revision=lease.authority_revision,
        fencing_token=lease.fencing_token,
        fencing_owner=lease.fencing_owner,
        durable_barrier_id=lease.durable_barrier_id,
        revision=lease.revision,
    )


def _scope_process(
    root_text: str,
    start: Any,
    starts: Any,
    ends: Any,
    index: int,
    crash: bool,
    validated: bool = False,
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
    recheck = lease_recheck(lease)
    scope = LockDomainScope(domain, session, fence, lease, recheck, identity(), locked)
    context = {
        "project_id": PROJECT,
        "authority_revision": "authority-1",
        "fencing_token": "fence-1",
        "fencing_owner": "owner-1",
        "durable_barrier_id": "barrier-1",
        "state_revision": 1,
    }
    start.wait()
    held = scope.validated_hold(context) if validated else scope.hold()
    with held:
        starts[index] = time.monotonic_ns()
        if crash:
            os._exit(17)
        time.sleep(0.05)
        ends[index] = time.monotonic_ns()


def _binding_abort_process(root_text: str, start: Any) -> None:
    """Abort a binding holder in a child process without touching its backend."""
    root = Path(root_text)
    control = SQLiteRollbackControlStore(
        root / "control.sqlite", PROJECT, root / "authority.sqlite"
    )
    session = SQLiteBarrierSessionStore(control, lambda: "authority-1")
    fence = MutationFence(
        root / "authority.sqlite",
        root / "authority-marker.json",
        root / "authority-lifecycle.json",
        root / "authority.lock",
        root / "control.sqlite",
        root / "control-binding.json",
        control.control_lock_path,
    )
    with locked() as guard:
        domain = LockDomainContract.capture(guard, session, fence)
    lease = AdmissionLease(PROJECT, "authority-1", "fence-1", "owner-1", "barrier-1", 1)
    recheck = validate_recheck(
        lease,
        project_id=PROJECT,
        authority_revision="authority-1",
        fencing_token="fence-1",  # noqa: S106
        fencing_owner="owner-1",
        durable_barrier_id="barrier-1",
        revision=1,
    )

    class Backend:
        def cas(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            raise AssertionError("aborted binding must not touch backend")

    binding = AdmittedControlBinding.bind(
        Backend(),
        lease,
        recheck,
        LockDomainScope(domain, session, fence, lease, recheck, identity(), locked),
    )
    start.wait()
    with binding.validated_scope():
        os._exit(29)


def _fresh_binding_rejection_process(root_text: str, result: Any) -> None:
    """Fresh worker must reject replaced durable identity before backend use."""
    root = Path(root_text)
    control = SQLiteRollbackControlStore(
        root / "control.sqlite", PROJECT, root / "authority.sqlite"
    )
    session = SQLiteBarrierSessionStore(control, lambda: "authority-after-crash")
    fence = MutationFence(
        root / "authority.sqlite",
        root / "authority-marker.json",
        root / "authority-lifecycle.json",
        root / "authority.lock",
        root / "control.sqlite",
        root / "control-binding.json",
        control.control_lock_path,
    )
    with locked() as guard:
        domain = LockDomainContract.capture(guard, session, fence)
    lease = AdmissionLease(PROJECT, "authority-1", "fence-1", "owner-1", "barrier-1", 1)
    recheck = validate_recheck(
        lease,
        project_id=PROJECT,
        authority_revision="authority-1",
        fencing_token="fence-1",  # noqa: S106
        fencing_owner="owner-1",
        durable_barrier_id="barrier-1",
        revision=1,
    )

    class Backend:
        def cas(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            raise AssertionError("rejected binding must not touch backend")

    binding = AdmittedControlBinding.bind(
        Backend(),
        lease,
        recheck,
        LockDomainScope(domain, session, fence, lease, recheck, identity(), locked),
    )
    try:
        with binding.validated_scope():
            result.put(("accepted", session.operation_owned_by_current_thread))
    except AdmissionLeaseError as error:
        result.put((str(error), not session.operation_owned_by_current_thread))


def _fresh_binding_reacquire_process(root_text: str, result: Any) -> None:
    """Fresh worker reacquires the unchanged durable scope after child death."""
    root = Path(root_text)
    control = SQLiteRollbackControlStore(
        root / "control.sqlite", PROJECT, root / "authority.sqlite"
    )
    session = SQLiteBarrierSessionStore(control, lambda: "authority-1")
    fence = MutationFence(
        root / "authority.sqlite",
        root / "authority-marker.json",
        root / "authority-lifecycle.json",
        root / "authority.lock",
        root / "control.sqlite",
        root / "control-binding.json",
        control.control_lock_path,
    )
    with locked() as guard:
        domain = LockDomainContract.capture(guard, session, fence)
    lease = AdmissionLease(PROJECT, "authority-1", "fence-1", "owner-1", "barrier-1", 1)
    recheck = validate_recheck(
        lease,
        project_id=PROJECT,
        authority_revision="authority-1",
        fencing_token="fence-1",  # noqa: S106
        fencing_owner="owner-1",
        durable_barrier_id="barrier-1",
        revision=1,
    )

    class Backend:
        def cas(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            raise AssertionError("reacquisition must not touch backend")

    binding = AdmittedControlBinding.bind(
        Backend(),
        lease,
        recheck,
        LockDomainScope(domain, session, fence, lease, recheck, identity(), locked),
    )
    try:
        with binding.validated_scope():
            result.put(("acquired", session.operation_owned_by_current_thread))
    except (AdmissionLeaseError, LockDomainError) as error:
        result.put((type(error).__name__, False))


def _fresh_recheck_process(
    root_text: str,
    result: Any,
    crash_after_reread: bool = False,
    crash_after_rejection: bool = False,
    fail_authority_reread: bool = False,
    fail_once_then_retry: bool = False,
    fail_on_second_reread: bool = False,
) -> None:
    """Perform a trusted reread in a fresh process, then reject the old lease."""
    root = Path(root_text)
    authority = root / "authority.sqlite"
    control = root / "control.sqlite"
    store = SQLiteRollbackControlStore(control, PROJECT, authority)

    authority_reads = 0

    def read_authority() -> str:
        nonlocal authority_reads
        authority_reads += 1
        if fail_authority_reread or (fail_once_then_retry and authority_reads == 1):
            raise RuntimeError("authority unavailable")
        if fail_on_second_reread and authority_reads == 2:
            raise RuntimeError("authority unavailable")
        return "authority-retry"

    session = SQLiteBarrierSessionStore(store, read_authority)
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
    recheck = lease_recheck(lease)
    rejected = True
    try:
        _reread_with_optional_transient_failure(session, fail_once_then_retry)
        if crash_after_reread:
            os._exit(19)
        scope = LockDomainScope(domain, session, fence, lease, recheck, identity(), locked)
        for _ in range(2):
            try:
                with scope.hold():
                    rejected = False
                    break
            except LockDomainError:
                if crash_after_rejection:
                    os._exit(23)
                rejected = True
    except ControlStoreError:
        rejected = True
    finally:
        result.put((rejected, not session.operation_owned_by_current_thread))


def _reread_with_optional_transient_failure(
    session: SQLiteBarrierSessionStore, fail_once_then_retry: bool
) -> None:
    for _ in range(2):
        try:
            session.recheck_held(1)
        except ControlStoreError:
            if not fail_once_then_retry:
                raise


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
        self.recheck = lease_recheck(self.lease)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_scope_proves_durable_session_inside_all_three_locks(self) -> None:
        scope = LockDomainScope(
            self.domain, self.session, self.fence, self.lease, self.recheck, identity(), locked
        )
        events: list[str] = []
        with scope.hold():
            events.append("held")
            self.assertTrue(self.session.operation_owned_by_current_thread)
        self.assertEqual(["held"], events)
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bound_observer_receives_immutable_reread_events(self) -> None:
        observed: list[LifecycleEvent] = []
        scope = LockDomainScope.bind(
            self.session,
            self.fence,
            self.lease,
            self.recheck,
            locked,
            lambda event: observed.append(event),
        )
        with scope.hold():
            pass
        self.assertGreaterEqual(len(observed), 2)
        self.assertTrue(all(event.phase == "scope.reread" for event in observed))
        self.assertEqual(1, observed[-1].revision)
        with self.assertRaises(AttributeError):
            observed[0].revision = 2  # type: ignore[misc]
        self.assertEqual(PROJECT, observed[0].project_id)
        self.assertEqual(identity().identity_digest, observed[0].session_digest)
        self.assertEqual("fence-1", observed[0].fencing_token)

    def test_lifecycle_event_cannot_be_forged_directly(self) -> None:
        with self.assertRaisesRegex(TypeError, "scope-issued"):
            LifecycleEvent(
                "scope.reread", 1, "owner-1", "authority", PROJECT, "digest", "fence", object()
            )

    def test_scope_events_map_to_model_actions_and_reject_mixed_tokens(self) -> None:
        first_token = object()
        second_token = object()
        event_fields = (1, "owner-1", "authority", PROJECT, "digest", "fence")
        first = _issue_event(first_token, "acquire", *event_fields)
        second = _issue_event(first_token, "quiesce", *event_fields)
        self.assertEqual(("Preflight", "Quiesce"), validate_model_trace((first, second)))
        foreign = _issue_event(second_token, "backup", *event_fields)
        with self.assertRaisesRegex(ValueError, "mixes scope"):
            validate_model_trace((first, foreign))
        with self.assertRaisesRegex(ValueError, "not mapped"):
            validate_model_trace((_issue_event(first_token, "scope.reread", *event_fields),))
        with self.assertRaisesRegex(ValueError, "not mapped"):
            validate_model_trace(
                (
                    _issue_event(first_token, "acquire", *event_fields),
                    _issue_event(first_token, "scope.reread", *event_fields),
                )
            )

    def test_model_trace_rejects_invalid_transition_order(self) -> None:
        token = object()
        event_fields = (1, "owner-1", "authority", PROJECT, "digest", "fence")
        events = (
            _issue_event(token, "acquire", *event_fields),
            _issue_event(token, "commit", *event_fields),
        )
        with self.assertRaisesRegex(ValueError, "transition is not allowed"):
            validate_model_trace(events)

    def test_model_trace_rejects_identity_and_revision_drift(self) -> None:
        token = object()
        fields = (2, "owner-1", "authority", PROJECT, "digest", "fence")
        with self.assertRaisesRegex(ValueError, "identity changed"):
            validate_model_trace(
                (
                    _issue_event(token, "acquire", *fields),
                    _issue_event(
                        token, "quiesce", 2, "owner-2", "authority", PROJECT, "digest", "fence"
                    ),
                )
            )
        with self.assertRaisesRegex(ValueError, "identity changed"):
            validate_model_trace(
                (
                    _issue_event(token, "acquire", *fields),
                    _issue_event(
                        token, "quiesce", 2, "owner-1", "foreign-lock", PROJECT, "digest", "fence"
                    ),
                )
            )
        with self.assertRaisesRegex(ValueError, "revision regressed"):
            validate_model_trace(
                (
                    _issue_event(token, "acquire", *fields),
                    _issue_event(
                        token, "quiesce", 1, "owner-1", "authority", PROJECT, "digest", "fence"
                    ),
                )
            )
        with self.assertRaisesRegex(ValueError, "revision is invalid"):
            validate_model_trace((_issue_event(token, "acquire", -1, *fields[1:]),))

    def test_model_action_contract_binds_authoritative_upgrade_model(self) -> None:
        validate_model_action_contract(Path(__file__).resolve().parents[1])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "formal/upgrade").mkdir(parents=True)
            (root / "formal/upgrade/UpgradeRecovery.tla").write_text(
                "Preflight(op) == TRUE\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "model actions are missing"):
                validate_model_action_contract(root)
            with self.assertRaisesRegex(ValueError, "model is unavailable"):
                validate_model_action_contract(root / "missing")
            event = _issue_event(
                object(), "acquire", 1, "owner-1", "authority", PROJECT, "digest", "fence"
            )
            with self.assertRaisesRegex(ValueError, "model actions are missing"):
                validate_model_trace((event,), model_root=root)
            (root / "formal/upgrade/UpgradeRecovery.tla").write_text(
                "Preflight(op) == TRUE\nBackupGit(op) == TRUE\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "Backup"):
                validate_model_action_contract(root)
            all_actions = "\n".join(
                f"{name}(op) == TRUE"
                for name in (
                    "Preflight",
                    "Quiesce",
                    "BackupGit",
                    "BackupSQLite",
                    "Stage",
                    "Commit",
                    "Validate",
                    "Reopen",
                    "StartRollback",
                    "VerifyRollback",
                    "ReleaseRollback",
                )
            )
            (root / "formal/upgrade/UpgradeRecovery.tla").write_text(all_actions, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invariant NoReplacementBeforeBackup"):
                validate_model_action_contract(root)
            all_but_availability = (
                all_actions
                + "\n"
                + "\n".join(
                    (
                        "NoReplacementBeforeBackup == TRUE",
                        "ReleaseOrder == TRUE",
                        "RollbackProof == TRUE",
                        "RollbackRequiresBackup == TRUE",
                    )
                )
            )
            (root / "formal/upgrade/UpgradeRecovery.tla").write_text(
                all_but_availability, encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "invariant FunctionalAvailability"):
                validate_model_action_contract(root)

    def test_terminal_recovery_contract_binds_barrier_model(self) -> None:
        validate_terminal_recovery_contract(Path(__file__).resolve().parents[1])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "formal/upgrade").mkdir(parents=True)
            (root / "formal/upgrade/HandoffctlUpgradeBarrier.tla").write_text(
                "VerifyTerminal(op, target) == TRUE\nterminalTarget\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "terminalVerified"):
                validate_terminal_recovery_contract(root)
        event = _issue_event(
            object(), "acquire", 1, "owner-1", "authority", PROJECT, "digest", "fence"
        )
        with patch(
            "tools.lifecycle_trace.validate_terminal_recovery_contract",
            side_effect=ValueError("terminal contract rejected"),
        ), self.assertRaisesRegex(ValueError, "terminal contract rejected"):
            validate_model_trace((event,))

    def test_model_trace_rejects_empty_invalid_and_malformed_events(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            validate_model_trace(())
        with self.assertRaisesRegex(ValueError, "invalid event"):
            validate_model_trace((object(),))  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "identity is invalid"):
            validate_model_trace(
                (_issue_event(object(), "acquire", 1, "", "authority", PROJECT, "digest", "fence"),)
            )

    def test_hold_performs_trusted_authority_reread_after_lock_acquisition(self) -> None:
        reads: list[str] = []

        def reread() -> str:
            reads.append("authority")
            return "authority-1"

        session = SQLiteBarrierSessionStore(self.store, reread)
        scope = LockDomainScope.bind(session, self.fence, self.lease, self.recheck, locked)
        with scope.hold():
            self.assertTrue(session.operation_owned_by_current_thread)
        self.assertEqual(["authority", "authority"], reads)

    def test_hold_rejects_trusted_authority_drift_between_rereads(self) -> None:
        reads = iter(("authority-1", "authority-2"))

        def reread() -> str:
            return next(reads)

        session = SQLiteBarrierSessionStore(self.store, reread)
        events: list[LifecycleEvent] = []
        scope = LockDomainScope.bind(
            session,
            self.fence,
            self.lease,
            self.recheck,
            locked,
            lambda event: events.append(event),
        )
        with self.assertRaisesRegex(LockDomainError, "authority revision changed"), scope.hold():
            self.fail("authority drift must reject before yielding the scope")
        self.assertEqual(1, len(events))
        self.assertFalse(session.operation_owned_by_current_thread)

    def test_hold_releases_locks_when_second_trusted_reread_fails(self) -> None:
        calls = 0

        def reread() -> str:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("authority unavailable")
            return "authority-1"

        session = SQLiteBarrierSessionStore(self.store, reread)
        scope = LockDomainScope.bind(session, self.fence, self.lease, self.recheck, locked)
        with self.assertRaisesRegex(LockDomainError, "recheck failed"), scope.hold():
            self.fail("failed trusted reread must not yield")
        self.assertEqual(2, calls)
        self.assertFalse(session.operation_owned_by_current_thread)

    def test_bind_captures_canonical_identity_before_hold(self) -> None:
        scope = LockDomainScope.bind(self.session, self.fence, self.lease, self.recheck, locked)
        with scope.hold():
            self.assertTrue(self.session.operation_owned_by_current_thread)
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_bind_rejects_invalid_caller_components(self) -> None:
        with self.assertRaisesRegex(LockDomainError, "session store"):
            LockDomainScope.bind(cast(Any, None), self.fence, self.lease, self.recheck, locked)
        with self.assertRaisesRegex(LockDomainError, "authority fence"):
            LockDomainScope.bind(self.session, cast(Any, None), self.lease, self.recheck, locked)
        with self.assertRaisesRegex(LockDomainError, "admission lease"):
            LockDomainScope.bind(self.session, self.fence, cast(Any, None), self.recheck, locked)
        other = AdmissionLease(PROJECT, "authority-1", "other", "owner-1", "barrier-1", 1)
        with self.assertRaisesRegex(LockDomainError, "admission recheck"):
            LockDomainScope.bind(self.session, self.fence, self.lease, lease_recheck(other), locked)

    def test_assert_ordered_rejects_invalid_recheck_and_session_identity(self) -> None:
        scope = LockDomainScope(
            self.domain, self.session, self.fence, self.lease, self.recheck, identity(), locked
        )
        scope._recheck = cast(AdmissionRecheck, object())
        with self.assertRaisesRegex(LockDomainError, "recheck"):
            scope.assert_ordered()
        scope._recheck = self.recheck
        scope._session_identity = cast(Any, object())
        with self.assertRaisesRegex(LockDomainError, "session identity"):
            scope.assert_ordered()

    def test_hold_rejects_non_guard_common_lock(self) -> None:
        @contextmanager
        def invalid_lock() -> Iterator[object]:
            yield object()

        scope = LockDomainScope(
            self.domain,
            self.session,
            self.fence,
            self.lease,
            self.recheck,
            identity(),
            cast(Any, invalid_lock),
        )
        with self.assertRaisesRegex(RuntimeError, "capability"), scope.hold():
            self.fail("invalid common lock must not yield")

    def test_validated_hold_rejects_context_before_acquiring_scope(self) -> None:
        scope = LockDomainScope.bind(self.session, self.fence, self.lease, self.recheck, locked)
        context = {
            "project_id": PROJECT,
            "authority_revision": "authority-1",
            "fencing_token": "fence-1",
            "fencing_owner": "owner-1",
            "durable_barrier_id": "barrier-1",
            "state_revision": 1,
        }
        with scope.validated_hold(context):
            self.assertTrue(self.session.operation_owned_by_current_thread)
        self.assertFalse(self.session.operation_owned_by_current_thread)
        for key in context:
            drifted = dict(context)
            drifted[key] = "drifted" if key != "state_revision" else 2
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(LockDomainError, "does not match"),
                scope.validated_hold(drifted),
            ):
                self.fail("invalid caller context must be rejected before hold")
            self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_scope_rejects_lease_drift_and_releases_control(self) -> None:
        for drifted in (
            AdmissionLease(PROJECT, "authority-1", "other", "owner-1", "barrier-1", 1),
            AdmissionLease(PROJECT, "authority-1", "fence-1", "owner-1", "barrier-1", 2),
        ):
            scope = LockDomainScope(
                self.domain,
                self.session,
                self.fence,
                drifted,
                lease_recheck(drifted),
                identity(),
                locked,
            )
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
        scope = LockDomainScope(
            self.domain, self.session, self.fence, self.lease, self.recheck, identity(), locked
        )
        with self.assertRaisesRegex(LockDomainError, "binding|identity"), scope.hold():
            self.fail("unreachable")

    def test_abort_then_recheck_rejects_replaced_authority(self) -> None:
        """A caller abort cannot make a replaced authority descriptor admissible."""
        scope = LockDomainScope(
            self.domain, self.session, self.fence, self.lease, self.recheck, identity(), locked
        )
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
        scope = LockDomainScope(
            self.domain, self.session, self.fence, self.lease, self.recheck, identity(), locked
        )
        with self.assertRaisesRegex(SystemExit, "simulated abort"), scope.hold():
            raise SystemExit("simulated abort")
        self.assertFalse(self.session.operation_owned_by_current_thread)

        self.session.mark_ambiguous(1, "caller-abort")
        with self.assertRaisesRegex(LockDomainError, "not held"), scope.hold():
            self.fail("ambiguous durable session must be rejected")
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_abort_then_recheck_rejects_replaced_session_identity(self) -> None:
        """A replaced durable authority revision cannot pass the old lease."""
        scope = LockDomainScope(
            self.domain, self.session, self.fence, self.lease, self.recheck, identity(), locked
        )
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

    def test_process_death_then_replaced_session_rejects_fresh_handoff(self) -> None:
        """A fresh process cannot inherit a dead caller's replaced session lease."""
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

        changed_record = replace(
            identity(), authority_revision_at_acquire="authority-after-crash", state_revision=2
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

        scope = LockDomainScope(
            self.domain, self.session, self.fence, self.lease, self.recheck, identity(), locked
        )
        with self.assertRaisesRegex(LockDomainError, "do not match"), scope.hold():
            self.fail("fresh handoff must reject replaced durable session")
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_binding_abort_releases_locks_and_fresh_worker_rejects_replaced_identity(self) -> None:
        """A child abort cannot leak locks or bless a replaced binding identity."""
        context = multiprocessing.get_context("fork")
        start = context.Event()
        crashed = context.Process(target=_binding_abort_process, args=(self.directory.name, start))
        crashed.start()
        start.set()
        crashed.join(5)
        self.assertEqual(29, crashed.exitcode)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        changed_record = replace(
            identity(), authority_revision_at_acquire="authority-after-crash", state_revision=2
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

        result = context.Queue()
        fresh = context.Process(
            target=_fresh_binding_rejection_process, args=(self.directory.name, result)
        )
        fresh.start()
        fresh.join(5)
        self.assertEqual(0, fresh.exitcode)
        self.assertEqual(("admitted control scope is invalid", True), result.get())
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_binding_abort_allows_fresh_worker_reacquisition_when_identity_is_unchanged(
        self,
    ) -> None:
        """An aborted holder releases SQLite locks for a clean fresh-worker retry."""
        context = multiprocessing.get_context("fork")
        start = context.Event()
        crashed = context.Process(target=_binding_abort_process, args=(self.directory.name, start))
        crashed.start()
        start.set()
        crashed.join(5)
        self.assertEqual(29, crashed.exitcode)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        result = context.Queue()
        fresh = context.Process(
            target=_fresh_binding_reacquire_process, args=(self.directory.name, result)
        )
        fresh.start()
        fresh.join(5)
        self.assertEqual(0, fresh.exitcode)
        self.assertEqual(("acquired", True), result.get())
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_repeated_handoff_rereads_authority_before_rejecting_stale_retry(self) -> None:
        """Repeated child death cannot bypass a trusted authority reread."""
        context = multiprocessing.get_context("fork")
        for _ in range(2):
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
            self.assertFalse(self.session.operation_owned_by_current_thread)

        self.assertEqual(self.session.recheck_held(1), self.session.snapshot())
        changed_record = replace(
            identity(), authority_revision_at_acquire="authority-retry", state_revision=2
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
                    1,
                    PROJECT,
                ),
            )
            connection.commit()

        with self.assertRaisesRegex(ControlStoreError, "authority revision changed"):
            self.session.recheck_held(1)
        scope = LockDomainScope(
            self.domain, self.session, self.fence, self.lease, self.recheck, identity(), locked
        )
        with self.assertRaisesRegex(LockDomainError, "do not match"), scope.hold():
            self.fail("first stale retry must be rejected")
        self.assertFalse(self.session.operation_owned_by_current_thread)

        fresh_session = SQLiteBarrierSessionStore(self.store, lambda: "authority-retry")
        self.assertEqual(
            "authority-retry", fresh_session.recheck_held(1).identity.authority_revision_at_acquire
        )
        fresh_scope = LockDomainScope(
            self.domain,
            fresh_session,
            self.fence,
            self.lease,
            self.recheck,
            identity(),
            locked,
        )
        for retry in range(2):
            with (
                self.subTest(retry=retry),
                self.assertRaisesRegex(LockDomainError, "do not match"),
                fresh_scope.hold(),
            ):
                self.fail("fresh authority reread must not bless the stale lease")
            self.assertFalse(fresh_session.operation_owned_by_current_thread)

    def test_fresh_process_reread_rejects_old_lease_after_durable_retry(self) -> None:
        """A fresh process cannot turn durable retry evidence into old-lease admission."""
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

        changed_record = replace(
            identity(), authority_revision_at_acquire="authority-retry", state_revision=2
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
                    1,
                    PROJECT,
                ),
            )
            connection.commit()

        reread_crashed = context.Process(
            target=_fresh_recheck_process,
            args=(self.directory.name, context.Queue(), True),
        )
        reread_crashed.start()
        reread_crashed.join(5)
        self.assertEqual(19, reread_crashed.exitcode)

        failure_result = context.Queue()
        reread_failed = context.Process(
            target=_fresh_recheck_process,
            args=(self.directory.name, failure_result, False, False, True),
        )
        reread_failed.start()
        reread_failed.join(5)
        self.assertEqual(0, reread_failed.exitcode)
        rejected, released = failure_result.get(timeout=1)
        self.assertTrue(rejected)
        self.assertTrue(released)

    def test_fresh_process_reread_recovers_then_rejects_stale_lease(self) -> None:
        """A transient reread failure cannot bless a stale lease on retry."""
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

        changed_record = replace(
            identity(), authority_revision_at_acquire="authority-retry", state_revision=2
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
                    1,
                    PROJECT,
                ),
            )
            connection.commit()

        result = context.Queue()
        recovered = context.Process(
            target=_fresh_recheck_process,
            args=(self.directory.name, result, False, False, False, True),
        )
        recovered.start()
        recovered.join(5)
        self.assertEqual(0, recovered.exitcode)
        rejected, released = result.get(timeout=1)
        self.assertTrue(rejected)
        self.assertTrue(released)

        second_failure_result = context.Queue()
        second_failure = context.Process(
            target=_fresh_recheck_process,
            args=(self.directory.name, second_failure_result, False, False, False, False, True),
        )
        second_failure.start()
        second_failure.join(5)
        self.assertEqual(0, second_failure.exitcode)
        rejected, released = second_failure_result.get(timeout=1)
        self.assertTrue(rejected)
        self.assertTrue(released)

        rejection_crashed = context.Process(
            target=_fresh_recheck_process,
            args=(self.directory.name, context.Queue(), False, True),
        )
        rejection_crashed.start()
        rejection_crashed.join(5)
        self.assertEqual(23, rejection_crashed.exitcode)

        rejection_crashed_again = context.Process(
            target=_fresh_recheck_process,
            args=(self.directory.name, context.Queue(), False, True),
        )
        rejection_crashed_again.start()
        rejection_crashed_again.join(5)
        self.assertEqual(23, rejection_crashed_again.exitcode)

        result = context.Queue()
        fresh = context.Process(target=_fresh_recheck_process, args=(self.directory.name, result))
        fresh.start()
        fresh.join(5)
        self.assertEqual(0, fresh.exitcode)
        rejected, released = result.get(timeout=1)
        self.assertTrue(rejected)
        self.assertTrue(released)

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

    def test_validated_hold_process_death_releases_scope_for_fresh_caller(self) -> None:
        context = multiprocessing.get_context("fork")
        start = context.Event()
        starts = context.Array("q", [0], lock=False)
        ends = context.Array("q", [0], lock=False)
        crashed = context.Process(
            target=_scope_process,
            args=(self.directory.name, start, starts, ends, 0, True, True),
        )
        crashed.start()
        start.set()
        crashed.join(5)
        self.assertEqual(17, crashed.exitcode)

        start = context.Event()
        recovered = context.Process(
            target=_scope_process,
            args=(self.directory.name, start, starts, ends, 0, False, True),
        )
        recovered.start()
        start.set()
        recovered.join(5)
        self.assertEqual(0, recovered.exitcode)
        self.assertGreater(ends[0], starts[0])

    def test_validated_hold_rejects_stale_context_before_common_lock(self) -> None:  # noqa: C901
        common_calls: list[str] = []

        @contextmanager
        def counted_lock() -> Any:
            common_calls.append("acquire")
            with locked() as guard:
                yield guard

        scope = LockDomainScope.bind(
            self.session, self.fence, self.lease, self.recheck, counted_lock
        )
        common_calls.clear()
        stale_context = {
            "project_id": PROJECT,
            "authority_revision": "authority-stale",
            "fencing_token": "fence-1",
            "fencing_owner": "owner-1",
            "durable_barrier_id": "barrier-1",
            "state_revision": 1,
        }
        with (
            self.assertRaisesRegex(LockDomainError, "must be a mapping"),
            scope.validated_hold(cast(Any, ["not-a-context"])),
        ):
            self.fail("non-mapping context must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(stale_context),
        ):
            self.fail("stale context must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        class ExplodingMapping(Mapping[str, object]):
            def __getitem__(self, key: str) -> object:
                raise KeyError(key)

            def __iter__(self) -> Iterator[str]:
                raise RuntimeError("keys unavailable")

            def __len__(self) -> int:
                return 0

        with (
            self.assertRaisesRegex(LockDomainError, "mapping is invalid"),
            scope.validated_hold(cast(Any, ExplodingMapping())),
        ):
            self.fail("hostile mapping must fail closed before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        class ExplodingValueMapping(Mapping[str, object]):
            _keys = (
                "project_id",
                "authority_revision",
                "fencing_token",
                "fencing_owner",
                "durable_barrier_id",
                "state_revision",
            )

            def __getitem__(self, key: str) -> object:
                raise RuntimeError(f"value unavailable: {key}")

            def __iter__(self) -> Iterator[str]:
                return iter(self._keys)

            def __len__(self) -> int:
                return len(self._keys)

        with (
            self.assertRaisesRegex(LockDomainError, "mapping values are invalid"),
            scope.validated_hold(cast(Any, ExplodingValueMapping())),
        ):
            self.fail("mapping value lookup failures must fail closed before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        class ExplodingGetMapping(dict[str, object]):
            def get(self, key: str, _default: object = None) -> object:
                raise RuntimeError(f"get unavailable: {key}")

        with (
            self.assertRaisesRegex(LockDomainError, "mapping values are invalid"),
            scope.validated_hold(cast(Any, ExplodingGetMapping(stale_context))),
        ):
            self.fail("mapping get failures must fail closed before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        class ExplodingEquality:
            def __eq__(self, other: object) -> bool:
                raise RuntimeError(f"comparison unavailable: {other}")

        equality_failure_context = dict(stale_context)
        equality_failure_context["project_id"] = ExplodingEquality()
        with (
            self.assertRaisesRegex(LockDomainError, "mapping values are invalid"),
            scope.validated_hold(equality_failure_context),
        ):
            self.fail("context equality failures must fail closed before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        extra_key_context = dict(stale_context)
        extra_key_context["unexpected"] = "not-authorized"
        with (
            self.assertRaisesRegex(LockDomainError, "unknown keys"),
            scope.validated_hold(extra_key_context),
        ):
            self.fail("unknown context keys must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        missing_project_context = dict(stale_context)
        missing_project_context.pop("project_id")
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(missing_project_context),
        ):
            self.fail("missing project identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        missing_authority_context = dict(stale_context)
        missing_authority_context.pop("authority_revision")
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(missing_authority_context),
        ):
            self.fail("missing authority revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        missing_barrier_context = dict(stale_context)
        missing_barrier_context.pop("durable_barrier_id")
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(missing_barrier_context),
        ):
            self.fail("missing durable barrier identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        missing_owner_context = dict(stale_context)
        missing_owner_context.pop("fencing_owner")
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(missing_owner_context),
        ):
            self.fail("missing fencing owner must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        missing_fence_context = dict(stale_context)
        missing_fence_context.pop("fencing_token")
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(missing_fence_context),
        ):
            self.fail("missing fencing token must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        missing_authority_context = dict(stale_context)
        missing_authority_context.pop("authority_revision")
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(missing_authority_context),
        ):
            self.fail("missing authority revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        replaced_authority_context = dict(stale_context)
        replaced_authority_context["authority_revision"] = "authority-2"
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(replaced_authority_context),
        ):
            self.fail("replaced authority revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        missing_revision_context = dict(stale_context)
        missing_revision_context.pop("state_revision")
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(missing_revision_context),
        ):
            self.fail("missing state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        decimal_revision_context = dict(stale_context)
        decimal_revision_context["state_revision"] = __import__("decimal").Decimal("1")
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(decimal_revision_context),
        ):
            self.fail("decimal state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        bytearray_revision_context = dict(stale_context)
        bytearray_revision_context["state_revision"] = bytearray(b"1")
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(bytearray_revision_context),
        ):
            self.fail("bytearray state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        memoryview_revision_context = dict(stale_context)
        memoryview_revision_context["state_revision"] = memoryview(b"1")
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(memoryview_revision_context),
        ):
            self.fail("memoryview state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        stale_revision_context = dict(stale_context)
        stale_revision_context["state_revision"] = 2
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(stale_revision_context),
        ):
            self.fail("stale state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        list_revision_context = dict(stale_context)
        list_revision_context["state_revision"] = [1, 2]
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(list_revision_context),
        ):
            self.fail("list state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        range_revision_context = dict(stale_context)
        range_revision_context["state_revision"] = range(1)
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(range_revision_context),
        ):
            self.fail("range state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        negative_inf_revision_context = dict(stale_context)
        negative_inf_revision_context["state_revision"] = float("-inf")
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(negative_inf_revision_context),
        ):
            self.fail("negative infinite state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        inf_revision_context = dict(stale_context)
        inf_revision_context["state_revision"] = float("inf")
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(inf_revision_context),
        ):
            self.fail("infinite state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        complex_revision_context = dict(stale_context)
        complex_revision_context["state_revision"] = complex(1, 0)
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(complex_revision_context),
        ):
            self.fail("complex state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        decimal_revision_context = dict(stale_context)
        decimal_revision_context["state_revision"] = 1.5
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(decimal_revision_context),
        ):
            self.fail("fractional state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        nan_revision_context = dict(stale_context)
        nan_revision_context["state_revision"] = float("nan")
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(nan_revision_context),
        ):
            self.fail("NaN state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        object_revision_context = dict(stale_context)
        object_revision_context["state_revision"] = object()
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(object_revision_context),
        ):
            self.fail("object state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        mapping_revision_context = dict(stale_context)
        mapping_revision_context["state_revision"] = {"revision": 1}
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(mapping_revision_context),
        ):
            self.fail("mapping state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        frozenset_revision_context = dict(stale_context)
        frozenset_revision_context["state_revision"] = frozenset({1})
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(frozenset_revision_context),
        ):
            self.fail("frozenset state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        bytes_revision_context = dict(stale_context)
        bytes_revision_context["state_revision"] = b"1"
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(bytes_revision_context),
        ):
            self.fail("bytes state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        set_revision_context = dict(stale_context)
        set_revision_context["state_revision"] = {1}
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(set_revision_context),
        ):
            self.fail("set state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        tuple_revision_context = dict(stale_context)
        tuple_revision_context["state_revision"] = (1,)
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(tuple_revision_context),
        ):
            self.fail("tuple state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        sequence_revision_context = dict(stale_context)
        sequence_revision_context["state_revision"] = [1]
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(sequence_revision_context),
        ):
            self.fail("sequence state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        null_revision_context = dict(stale_context)
        null_revision_context["state_revision"] = None
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(null_revision_context),
        ):
            self.fail("null state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        string_revision_context = dict(stale_context)
        string_revision_context["state_revision"] = "1"
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(string_revision_context),
        ):
            self.fail("string state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        float_revision_context = dict(stale_context)
        float_revision_context["state_revision"] = 1.0
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(float_revision_context),
        ):
            self.fail("float state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        negative_revision_context = dict(stale_context)
        negative_revision_context["state_revision"] = -1
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(negative_revision_context),
        ):
            self.fail("negative state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        zero_revision_context = dict(stale_context)
        zero_revision_context["state_revision"] = 0
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(zero_revision_context),
        ):
            self.fail("zero state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        bool_state_context = dict(stale_context)
        bool_state_context["state_revision"] = True
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(bool_state_context),
        ):
            self.fail("boolean state revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        empty_project_context = dict(stale_context)
        empty_project_context["project_id"] = ""
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(empty_project_context),
        ):
            self.fail("empty project identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        null_barrier_context = dict(stale_context)
        null_barrier_context["durable_barrier_id"] = None
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(null_barrier_context),
        ):
            self.fail("null durable barrier identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        empty_barrier_context = dict(stale_context)
        empty_barrier_context["durable_barrier_id"] = ""
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(empty_barrier_context),
        ):
            self.fail("empty durable barrier identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        null_owner_context = dict(stale_context)
        null_owner_context["fencing_owner"] = None
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(null_owner_context),
        ):
            self.fail("null fencing owner must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        empty_owner_context = dict(stale_context)
        empty_owner_context["fencing_owner"] = ""
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(empty_owner_context),
        ):
            self.fail("empty fencing owner must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        non_text_owner_context = dict(stale_context)
        non_text_owner_context["fencing_owner"] = 9
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(non_text_owner_context),
        ):
            self.fail("non-text fencing owner must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        bool_owner_context = dict(stale_context)
        bool_owner_context["fencing_owner"] = True
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(bool_owner_context),
        ):
            self.fail("boolean fencing owner must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        list_owner_context = dict(stale_context)
        list_owner_context["fencing_owner"] = ["owner-1"]
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(list_owner_context),
        ):
            self.fail("list fencing owner must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        tuple_owner_context = dict(stale_context)
        tuple_owner_context["fencing_owner"] = ("owner-1",)
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(tuple_owner_context),
        ):
            self.fail("tuple fencing owner must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        bytes_owner_context = dict(stale_context)
        bytes_owner_context["fencing_owner"] = b"owner-1"
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(bytes_owner_context),
        ):
            self.fail("bytes fencing owner must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        map_owner_context = dict(stale_context)
        map_owner_context["fencing_owner"] = {"owner": "owner-1"}
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(map_owner_context),
        ):
            self.fail("mapping fencing owner must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        bytearray_owner_context = dict(stale_context)
        bytearray_owner_context["fencing_owner"] = bytearray(b"owner-1")
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(bytearray_owner_context),
        ):
            self.fail("bytearray fencing owner must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        missing_context = dict(stale_context)
        missing_context.pop("fencing_owner")
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(missing_context),
        ):
            self.fail("missing caller context must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        memoryview_owner_context = dict(stale_context)
        memoryview_owner_context["fencing_owner"] = memoryview(b"owner-1")
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(memoryview_owner_context),
        ):
            self.fail("memoryview fencing owner must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        bool_revision_context = dict(stale_context)
        bool_revision_context["authority_revision"] = "authority-1"
        bool_revision_context["state_revision"] = True
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(bool_revision_context),
        ):
            self.fail("boolean revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        non_text_context = dict(stale_context)
        non_text_context["authority_revision"] = 1
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(non_text_context),
        ):
            self.fail("non-text authority revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        bool_authority_context = dict(stale_context)
        bool_authority_context["authority_revision"] = True
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(bool_authority_context),
        ):
            self.fail("boolean authority revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        list_authority_context = dict(stale_context)
        list_authority_context["authority_revision"] = ["authority-stale"]
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(list_authority_context),
        ):
            self.fail("list authority revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        tuple_authority_context = dict(stale_context)
        tuple_authority_context["authority_revision"] = ("authority-stale",)
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(tuple_authority_context),
        ):
            self.fail("tuple authority revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        bytes_authority_context = dict(stale_context)
        bytes_authority_context["authority_revision"] = b"authority-stale"
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(bytes_authority_context),
        ):
            self.fail("bytes authority revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        memoryview_authority_context = dict(stale_context)
        memoryview_authority_context["authority_revision"] = memoryview(b"authority-stale")
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(memoryview_authority_context),
        ):
            self.fail("memoryview authority revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        frozenset_authority_context = dict(stale_context)
        frozenset_authority_context["authority_revision"] = frozenset({"authority-stale"})
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(frozenset_authority_context),
        ):
            self.fail("frozenset authority revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        map_authority_context = dict(stale_context)
        map_authority_context["authority_revision"] = {"revision": "authority-stale"}
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(map_authority_context),
        ):
            self.fail("mapping authority revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        bytearray_authority_context = dict(stale_context)
        bytearray_authority_context["authority_revision"] = bytearray(b"authority-stale")
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(bytearray_authority_context),
        ):
            self.fail("bytearray authority revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        non_text_project_context = dict(stale_context)
        non_text_project_context["project_id"] = 111
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(non_text_project_context),
        ):
            self.fail("non-text project identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        null_project_context = dict(stale_context)
        null_project_context["project_id"] = None
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(null_project_context),
        ):
            self.fail("null project identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        bool_project_context = dict(stale_context)
        bool_project_context["project_id"] = True
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(bool_project_context),
        ):
            self.fail("boolean project identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        map_project_context = dict(stale_context)
        map_project_context["project_id"] = {"project": PROJECT}
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(map_project_context),
        ):
            self.fail("mapping project identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        sequence_project_context = dict(stale_context)
        sequence_project_context["project_id"] = [PROJECT]
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(sequence_project_context),
        ):
            self.fail("sequence project identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        tuple_project_context = dict(stale_context)
        tuple_project_context["project_id"] = (PROJECT,)
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(tuple_project_context),
        ):
            self.fail("tuple project identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        bytes_project_context = dict(stale_context)
        bytes_project_context["project_id"] = PROJECT.encode()
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(bytes_project_context),
        ):
            self.fail("bytes project identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        memoryview_project_context = dict(stale_context)
        memoryview_project_context["project_id"] = memoryview(PROJECT.encode())
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(memoryview_project_context),
        ):
            self.fail("memoryview project identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        set_project_context = dict(stale_context)
        set_project_context["project_id"] = {PROJECT}
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(set_project_context),
        ):
            self.fail("set project identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        frozenset_project_context = dict(stale_context)
        frozenset_project_context["project_id"] = frozenset({PROJECT})
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(frozenset_project_context),
        ):
            self.fail("frozenset project identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        bytearray_project_context = dict(stale_context)
        bytearray_project_context["project_id"] = bytearray(PROJECT, "utf-8")
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(bytearray_project_context),
        ):
            self.fail("bytearray project identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        non_text_fence_context = dict(stale_context)
        non_text_fence_context["fencing_token"] = 7
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(non_text_fence_context),
        ):
            self.fail("non-text fencing token must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        memoryview_fence_context = dict(stale_context)
        memoryview_fence_context["fencing_token"] = memoryview(b"fence-1")
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(memoryview_fence_context),
        ):
            self.fail("memoryview fencing token must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        bool_barrier_context = dict(stale_context)
        bool_barrier_context["durable_barrier_id"] = True
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(bool_barrier_context),
        ):
            self.fail("boolean durable barrier identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        bool_fence_context = dict(stale_context)
        bool_fence_context["fencing_token"] = True
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(bool_fence_context),
        ):
            self.fail("boolean fencing token must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        list_fence_context = dict(stale_context)
        list_fence_context["fencing_token"] = ["fence-1"]
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(list_fence_context),
        ):
            self.fail("list fencing token must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        tuple_fence_context = dict(stale_context)
        tuple_fence_context["fencing_token"] = ("fence-1",)
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(tuple_fence_context),
        ):
            self.fail("tuple fencing token must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        list_barrier_context = dict(stale_context)
        list_barrier_context["durable_barrier_id"] = ["barrier-1"]
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(list_barrier_context),
        ):
            self.fail("list durable barrier identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        tuple_barrier_context = dict(stale_context)
        tuple_barrier_context["durable_barrier_id"] = ("barrier-1",)
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(tuple_barrier_context),
        ):
            self.fail("tuple durable barrier identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        bytes_barrier_context = dict(stale_context)
        bytes_barrier_context["durable_barrier_id"] = b"barrier-1"
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(bytes_barrier_context),
        ):
            self.fail("bytes durable barrier identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        frozenset_barrier_context = dict(stale_context)
        frozenset_barrier_context["durable_barrier_id"] = frozenset({"barrier-1"})
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(frozenset_barrier_context),
        ):
            self.fail("frozenset durable barrier identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        map_fence_context = dict(stale_context)
        map_fence_context["fencing_token"] = {"token": "fence-1"}
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(map_fence_context),
        ):
            self.fail("mapping fencing token must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        map_barrier_context = dict(stale_context)
        map_barrier_context["durable_barrier_id"] = {"barrier": "barrier-1"}
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(map_barrier_context),
        ):
            self.fail("mapping durable barrier identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        bytearray_barrier_context = dict(stale_context)
        bytearray_barrier_context["durable_barrier_id"] = bytearray(b"barrier-1")
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(bytearray_barrier_context),
        ):
            self.fail("bytearray durable barrier identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        memoryview_barrier_context = dict(stale_context)
        memoryview_barrier_context["durable_barrier_id"] = memoryview(b"barrier-1")
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(memoryview_barrier_context),
        ):
            self.fail("memoryview durable barrier identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        non_text_barrier_context = dict(stale_context)
        non_text_barrier_context["durable_barrier_id"] = 13
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(non_text_barrier_context),
        ):
            self.fail("non-text barrier identifier must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        null_authority_context = dict(stale_context)
        null_authority_context["authority_revision"] = None
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(null_authority_context),
        ):
            self.fail("null authority revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        empty_authority_context = dict(stale_context)
        empty_authority_context["authority_revision"] = ""
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(empty_authority_context),
        ):
            self.fail("empty authority revision must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        null_fence_context = dict(stale_context)
        null_fence_context["fencing_token"] = None
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(null_fence_context),
        ):
            self.fail("null fencing token must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

        empty_fence_context = dict(stale_context)
        empty_fence_context["fencing_token"] = ""
        with (
            self.assertRaisesRegex(LockDomainError, "does not match"),
            scope.validated_hold(empty_fence_context),
        ):
            self.fail("empty fencing token must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)

    def test_validated_hold_rejects_unknown_key_from_mapping_adapter(self) -> None:
        common_calls: list[str] = []

        @contextmanager
        def counted_lock() -> Any:
            common_calls.append("acquire")
            with locked() as guard:
                yield guard

        class ExtraKeyMapping(Mapping[str, object]):
            def __init__(self) -> None:
                self.payload = {
                    "project_id": PROJECT,
                    "authority_revision": "authority-1",
                    "fencing_token": "fence-1",
                    "fencing_owner": "owner-1",
                    "durable_barrier_id": "barrier-1",
                    "state_revision": 1,
                    "unexpected": "not-authorized",
                }

            def __getitem__(self, key: str) -> object:
                return self.payload[key]

            def __iter__(self) -> Iterator[str]:
                return iter(self.payload)

            def __len__(self) -> int:
                return len(self.payload)

        scope = LockDomainScope.bind(
            self.session, self.fence, self.lease, self.recheck, counted_lock
        )
        common_calls.clear()
        with (
            self.assertRaisesRegex(LockDomainError, "unknown keys"),
            scope.validated_hold(ExtraKeyMapping()),
        ):
            self.fail("unknown adapter keys must fail before scope acquisition")
        self.assertEqual([], common_calls)
        self.assertFalse(self.session.operation_owned_by_current_thread)


if __name__ == "__main__":
    unittest.main()
