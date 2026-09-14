# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Uncalled concrete common/control/authority admission scope."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

from tools.admission_lease import LOCK_ORDER, AdmissionLease
from tools.handoffctl import CoordinatorLockGuard
from tools.lock_domain import LockDomainIdentity
from tools.mutation_fence import MutationFence
from tools.rollback_control_store import SQLiteBarrierSessionStore


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
        common_lock: Callable[[], AbstractContextManager[CoordinatorLockGuard]],
    ) -> None:
        self._identity = identity
        self._session_store = session_store
        self._authority_fence = authority_fence
        self._lease = lease
        self._common_lock = common_lock

    def assert_ordered(self) -> None:
        if LOCK_ORDER != ("common", "control", "authority"):
            raise RuntimeError("admission lock order is invalid")

    @contextmanager
    def hold(self) -> Iterator[object]:
        """Acquire and prove the full scope, releasing every lock on failure."""
        self.assert_ordered()
        with self._common_lock() as common_guard:
            if not isinstance(common_guard, CoordinatorLockGuard):
                raise RuntimeError("common lock capability is invalid")
            self._identity.assert_current(common_guard, self._session_store, self._authority_fence)
            with (
                self._session_store.lock_owned_by_caller(common_guard),
                self._authority_fence.locked(),
            ):
                self._identity.assert_current(
                    common_guard, self._session_store, self._authority_fence
                )
                state = self._session_store.snapshot_owned_by_caller()
                self._identity.assert_session_binding(state, self._lease)
                yield object()
