# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Uncalled concrete common/control/authority admission scope."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager

from tools.admission_lease import LOCK_ORDER, AdmissionLease, AdmissionRecheck
from tools.handoffctl import CoordinatorLockGuard
from tools.lifecycle_trace import LifecycleObserver, _issue_event
from tools.lock_domain import LockDomainContract, LockDomainError, LockDomainIdentity
from tools.mutation_fence import MutationFence
from tools.rollback_control_store import ControlStoreError, SQLiteBarrierSessionStore
from tools.upgrade_identity import BarrierSessionIdentity

LockDomainObserver = Callable[[str], None]


class LockDomainScope:
    """Caller-owned scope proving one durable session under one lock domain.

    This is deliberately not wired to CAS, selector publication, or upgrade
    execution.  It acquires common -> control -> authority, rereads the
    durable session while control is held, and then validates the lease.
    """

    def __init__(
        self,
        identity: LockDomainIdentity,
        session_store: SQLiteBarrierSessionStore,
        authority_fence: MutationFence,
        lease: AdmissionLease,
        recheck: AdmissionRecheck,
        session_identity: BarrierSessionIdentity,
        common_lock: Callable[[], AbstractContextManager[CoordinatorLockGuard]],
        session_revision: int | None = None,
        observer: LifecycleObserver | None = None,
        lock_observer: LockDomainObserver | None = None,
    ) -> None:
        self._identity = identity
        self._session_store = session_store
        self._authority_fence = authority_fence
        self._lease = lease
        self._recheck = recheck
        self._session_identity = session_identity
        self._common_lock = common_lock
        # Lease revision names the identity fence; session_revision names the
        # durable row CAS revision and can diverge after reconciliation.
        self._session_revision = lease.revision if session_revision is None else session_revision
        self._observer = observer
        self._lock_observer = lock_observer
        self._event_token = object()

    @classmethod
    def bind(
        cls,
        session_store: SQLiteBarrierSessionStore,
        authority_fence: MutationFence,
        lease: AdmissionLease,
        recheck: AdmissionRecheck,
        common_lock: Callable[[], AbstractContextManager[CoordinatorLockGuard]],
        observer: LifecycleObserver | None = None,
        lock_observer: LockDomainObserver | None = None,
    ) -> LockDomainScope:
        """Bind a caller-owned scope to canonical descriptors only.

        ``hold`` performs the immediate durable-session and lease recheck.
        This seam is intentionally not connected to mutation or dispatch.
        """
        if not isinstance(session_store, SQLiteBarrierSessionStore):
            raise LockDomainError("session store is invalid")
        if not isinstance(authority_fence, MutationFence):
            raise LockDomainError("authority fence is invalid")
        if not isinstance(lease, AdmissionLease):
            raise LockDomainError("admission lease is invalid")
        if not isinstance(recheck, AdmissionRecheck) or recheck.lease != lease:
            raise LockDomainError("admission recheck is invalid")
        with common_lock() as common_guard:
            identity = LockDomainContract.capture(common_guard, session_store, authority_fence)
            with session_store.lock_owned_by_caller(common_guard):
                state = session_store.snapshot_owned_by_caller()
            identity.assert_session_binding(state, lease, session_revision=state.revision)
        return cls(
            identity,
            session_store,
            authority_fence,
            lease,
            recheck,
            state.identity,
            common_lock,
            state.revision,
            observer,
            lock_observer,
        )

    def assert_ordered(self) -> None:
        if LOCK_ORDER != ("common", "control", "authority"):
            raise RuntimeError("admission lock order is invalid")
        if not isinstance(self._recheck, AdmissionRecheck) or self._recheck.lease != self._lease:
            raise LockDomainError("admission recheck does not match lease")
        if not isinstance(self._session_identity, BarrierSessionIdentity):
            raise LockDomainError("durable session identity is invalid")

    def assert_context(self, context: Mapping[str, object]) -> None:
        """Reject engine context whose immutable lease identity has drifted."""
        if not isinstance(context, Mapping):
            raise LockDomainError("engine context must be a mapping")
        required = {
            "project_id": self._lease.project_id,
            "authority_revision": self._lease.authority_revision,
            "fencing_token": self._lease.fencing_token,
            "fencing_owner": self._lease.fencing_owner,
            "durable_barrier_id": self._lease.durable_barrier_id,
            "state_revision": self._lease.revision,
        }
        try:
            context_keys = set(context)
        except Exception as error:
            raise LockDomainError("engine context mapping is invalid") from error
        if context_keys - set(required):
            raise LockDomainError("engine context schema contains unknown keys")
        try:
            values_match = any(
                context.get(key) != value or type(context.get(key)) is not type(value)
                for key, value in required.items()
            )
        except Exception as error:
            raise LockDomainError("engine context mapping values are invalid") from error
        if values_match:
            raise LockDomainError("engine context does not match admission lease")

    @contextmanager
    def validated_hold(self, context: Mapping[str, object]) -> Iterator[object]:
        """Validate caller context before acquiring the canonical scope.

        This is a rejection-only boundary for future adapters. It performs no
        CAS, selector publication, upgrade, apply, or rollback operation.
        """
        self.assert_context(context)
        with self.hold():
            yield object()

    @contextmanager
    def hold(self) -> Iterator[object]:
        """Acquire and prove the full scope, releasing every lock on failure."""
        self.assert_ordered()
        with self._common_lock() as common_guard:
            if not isinstance(common_guard, CoordinatorLockGuard):
                raise RuntimeError("common lock capability is invalid")
            self._observe_lock("AcquireCommon")
            self._identity.assert_current(common_guard, self._session_store, self._authority_fence)
            try:
                with self._session_store.lock_owned_by_caller(common_guard):
                    self._observe_lock("AcquireControl")
                    try:
                        self._recheck_session(common_guard)
                        with self._authority_fence.locked():
                            self._observe_lock("AcquireAuthority")
                            try:
                                self._identity.assert_current(
                                    common_guard, self._session_store, self._authority_fence
                                )
                                self._recheck_session(common_guard)
                                yield object()
                            finally:
                                self._observe_lock("ReleaseAuthority")
                    finally:
                        self._observe_lock("ReleaseControl")
            finally:
                self._observe_lock("ReleaseCommon")

    def _observe_lock(self, action: str) -> None:
        if self._lock_observer is not None:
            self._lock_observer(action)

    def _recheck_session(self, common_guard: CoordinatorLockGuard) -> None:
        """Reread trusted session evidence while the caller owns control locks."""
        observed = self._session_store.snapshot_owned_by_caller()
        if observed.status != "held":
            raise LockDomainError("durable session is not held")
        if (
            observed.identity != self._session_identity
            or observed.revision != self._session_revision
        ):
            raise LockDomainError("durable session and lease do not match")
        try:
            state = self._session_store.recheck_held_locked(
                common_guard,
                self._session_identity,
                self._session_revision,
            )
        except ControlStoreError as error:
            raise LockDomainError(f"durable session recheck failed: {error}") from error
        if state.status != "held":
            raise LockDomainError("durable session is not held")
        if state.identity != self._session_identity or state.revision != self._session_revision:
            raise LockDomainError("durable session and lease do not match")
        self._identity.assert_session_binding(
            state, self._lease, session_revision=self._session_revision
        )
        if self._observer is not None:
            self._observer(
                _issue_event(
                    self._event_token,
                    "scope.reread",
                    state.revision,
                    self._lease.fencing_owner,
                    "authority",
                    self._lease.project_id,
                    state.identity.identity_digest,
                    self._lease.fencing_token,
                )
            )
