# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the uncalled upgrade-engine scope adapter boundary."""

from __future__ import annotations

import unittest
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, cast

from tools.lock_domain import LockDomainIdentity
from tools.rollback_control_store import BarrierSessionState
from tools.scoped_backend_adapter import ScopedBackendAdapter
from tools.upgrade_engine import BackendAdapter as EngineBackendAdapter


class Scope:
    def __init__(self) -> None:
        self.events: list[str] = []

    def assert_ordered(self) -> None:
        self.events.append("ordered")

    def assert_context(self, _context: Mapping[str, object]) -> None:
        self.events.append("context")

    @contextmanager
    def hold(self) -> Iterator[object]:
        self.events.append("held")
        try:
            yield object()
        finally:
            self.events.append("released")


class Backend:
    def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, object]:
        return {"phase": phase, "context": dict(context)}

    def execute(self, phase: str, context: Mapping[str, object]) -> dict[str, object]:
        return {"phase": phase, "context": dict(context), "mutates_authority": False}

    def verify_rollback_context(self, context: Mapping[str, object]) -> dict[str, object]:
        return {"context": dict(context), "verified": True}


class ScopedBackendAdapterTests(unittest.TestCase):
    def test_adapter_satisfies_upgrade_engine_interface_without_wiring_dispatch(self) -> None:
        self.assertIsInstance(ScopedBackendAdapter(Backend(), Scope()), EngineBackendAdapter)

    def test_snapshot_and_execute_share_fail_closed_scope_boundary(self) -> None:
        scope = Scope()
        adapter = ScopedBackendAdapter(Backend(), scope)
        self.assertEqual("preflight", adapter.snapshot("preflight", {"x": 1})["phase"])
        self.assertFalse(adapter.execute("reopen", {"x": 2})["mutates_authority"])
        rollback = adapter.verify_rollback_context({"x": 3})
        self.assertIsNotNone(rollback)
        self.assertTrue(cast(dict[str, object], rollback)["verified"])
        self.assertEqual(
            [
                "context",
                "held",
                "released",
                "context",
                "held",
                "released",
                "context",
                "held",
                "released",
            ],
            scope.events,
        )

    def test_scope_failure_prevents_backend_call(self) -> None:
        class FailingScope(Scope):
            def assert_context(self, _context: Mapping[str, object]) -> None:
                pass

            @contextmanager
            def hold(self) -> Iterator[object]:
                if not self.events:
                    raise RuntimeError("stale lease")
                yield object()

        class UnexpectedBackend(Backend):
            def snapshot(self, _phase: str, _context: Mapping[str, object]) -> dict[str, object]:
                raise AssertionError("backend must not run")

        adapter = ScopedBackendAdapter(UnexpectedBackend(), FailingScope())
        with self.assertRaisesRegex(RuntimeError, "stale lease"):
            adapter.snapshot("preflight", {})

    def test_context_failure_prevents_scope_and_backend_call(self) -> None:
        class ContextScope(Scope):
            def assert_context(self, _context: Mapping[str, object]) -> None:
                raise RuntimeError("lease context drift")

        class UnexpectedBackend(Backend):
            def snapshot(self, _phase: str, _context: Mapping[str, object]) -> dict[str, object]:
                raise AssertionError("backend must not run")

        adapter = ScopedBackendAdapter(UnexpectedBackend(), ContextScope())
        with self.assertRaisesRegex(RuntimeError, "context drift"):
            adapter.snapshot("preflight", {})

    def test_rollback_verification_failure_prevents_backend_result_use(self) -> None:
        class FailingBackend(Backend):
            def verify_rollback_context(self, _context: Mapping[str, object]) -> dict[str, object]:
                raise RuntimeError("recheck failed")

        adapter = ScopedBackendAdapter(FailingBackend(), Scope())
        with self.assertRaisesRegex(RuntimeError, "recheck failed"):
            adapter.verify_rollback_context({})

    def test_execute_rejects_authority_mutation_at_uncalled_boundary(self) -> None:
        class MutatingBackend(Backend):
            def execute(self, _phase: str, _context: Mapping[str, object]) -> dict[str, object]:
                return {"mutates_authority": True}

        adapter = ScopedBackendAdapter(MutatingBackend(), Scope())
        with self.assertRaisesRegex(TypeError, "non-mutating"):
            adapter.execute("reopen", {})

    def test_execute_rejects_disabled_mutation_phases_before_scope(self) -> None:
        class UnexpectedScope(Scope):
            def assert_context(self, _context: Mapping[str, object]) -> None:
                raise AssertionError("disabled phase must stop before scope")

        adapter = ScopedBackendAdapter(Backend(), UnexpectedScope())
        for phase in ("commit", "apply", "rollback"):
            with self.subTest(phase=phase), self.assertRaisesRegex(TypeError, "disabled"):
                adapter.execute(phase, {})

    def test_invalid_scope_is_rejected_before_engine_binding(self) -> None:
        with self.assertRaisesRegex(TypeError, "scope"):
            ScopedBackendAdapter(Backend(), object())  # type: ignore[arg-type]

    def test_incomplete_backend_is_rejected_before_engine_binding(self) -> None:
        class IncompleteBackend:
            def snapshot(self, _phase: str, _context: Mapping[str, object]) -> dict[str, object]:
                return {}

            def execute(self, _phase: str, _context: Mapping[str, object]) -> dict[str, object]:
                return {}

        with self.assertRaisesRegex(TypeError, "incomplete"):
            ScopedBackendAdapter(IncompleteBackend(), Scope())  # type: ignore[arg-type]

    def test_session_entry_rejects_missing_typed_identity(self) -> None:
        with self.assertRaisesRegex(TypeError, "identity"):
            ScopedBackendAdapter.from_validated_session(
                Backend(),
                Scope(),
                cast(LockDomainIdentity, object()),
                object(),
                cast(BarrierSessionState, object()),
                cast(Any, object()),
            )

    def test_session_entry_rejects_invalid_lock_order(self) -> None:
        class BadOrderScope(Scope):
            def assert_ordered(self) -> None:
                raise RuntimeError("admission lock order is invalid")

        identity = object.__new__(LockDomainIdentity)
        with self.assertRaisesRegex(RuntimeError, "lock order"):
            ScopedBackendAdapter.from_validated_session(
                Backend(),
                BadOrderScope(),
                identity,
                object(),
                cast(BarrierSessionState, object()),
                cast(Any, object()),
            )

    def test_rechecked_entry_holds_scope_before_identity_validation(self) -> None:
        scope = Scope()
        with self.assertRaisesRegex(TypeError, "identity"):
            ScopedBackendAdapter.from_rechecked_session(
                Backend(),
                scope,
                cast(LockDomainIdentity, object()),
                object(),
                cast(BarrierSessionState, object()),
                cast(Any, object()),
            )
        self.assertEqual(["held", "released"], scope.events)

    def test_rechecked_entry_unwinds_scope_on_process_abort(self) -> None:
        class AbortScope(Scope):
            @contextmanager
            def hold(self) -> Iterator[object]:
                self.events.append("held")
                try:
                    raise SystemExit("simulated process death")
                finally:
                    self.events.append("released")

        scope = AbortScope()
        with self.assertRaisesRegex(SystemExit, "process death"):
            ScopedBackendAdapter.from_rechecked_session(
                Backend(),
                scope,
                cast(LockDomainIdentity, object()),
                object(),
                cast(BarrierSessionState, object()),
                cast(Any, object()),
            )
        self.assertEqual(["held", "released"], scope.events)

    def test_disabled_mutation_rejects_before_stale_context_recheck(self) -> None:
        class StaleScope(Scope):
            def assert_context(self, _context: Mapping[str, object]) -> None:
                raise AssertionError("stale context must not be reached")

        adapter = ScopedBackendAdapter(Backend(), StaleScope())
        with self.assertRaisesRegex(TypeError, "disabled"):
            adapter.execute("rollback", {})

    def test_recheck_abort_releases_scope_before_retry(self) -> None:
        class OneShotAbortScope(Scope):
            def __init__(self) -> None:
                super().__init__()
                self.aborted = False

            @contextmanager
            def hold(self) -> Iterator[object]:
                self.events.append("held")
                try:
                    if not self.aborted:
                        self.aborted = True
                        raise SystemExit("simulated abort")
                    yield object()
                finally:
                    self.events.append("released")

        scope = OneShotAbortScope()
        with self.assertRaisesRegex(SystemExit, "abort"):
            ScopedBackendAdapter.from_rechecked_session(
                Backend(),
                scope,
                cast(LockDomainIdentity, object()),
                object(),
                cast(BarrierSessionState, object()),
                cast(Any, object()),
            )
        with self.assertRaisesRegex(TypeError, "identity"):
            ScopedBackendAdapter.from_rechecked_session(
                Backend(),
                scope,
                cast(LockDomainIdentity, object()),
                object(),
                cast(BarrierSessionState, object()),
                cast(Any, object()),
            )
        self.assertEqual(["held", "released", "held", "released"], scope.events)


if __name__ == "__main__":
    unittest.main()
