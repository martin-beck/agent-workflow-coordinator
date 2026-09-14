# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the uncalled upgrade-engine scope adapter boundary."""

from __future__ import annotations

import unittest
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import cast

from tools.scoped_backend_adapter import ScopedBackendAdapter


class Scope:
    def __init__(self) -> None:
        self.events: list[str] = []

    def assert_ordered(self) -> None:
        self.events.append("ordered")

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
    def test_snapshot_and_execute_share_fail_closed_scope_boundary(self) -> None:
        scope = Scope()
        adapter = ScopedBackendAdapter(Backend(), scope)
        self.assertEqual("preflight", adapter.snapshot("preflight", {"x": 1})["phase"])
        self.assertFalse(adapter.execute("reopen", {"x": 2})["mutates_authority"])
        rollback = adapter.verify_rollback_context({"x": 3})
        self.assertIsNotNone(rollback)
        self.assertTrue(cast(dict[str, object], rollback)["verified"])
        self.assertEqual(
            ["held", "released", "held", "released", "held", "released"],
            scope.events,
        )

    def test_scope_failure_prevents_backend_call(self) -> None:
        class FailingScope(Scope):
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

    def test_rollback_verification_failure_prevents_backend_result_use(self) -> None:
        class FailingBackend(Backend):
            def verify_rollback_context(self, _context: Mapping[str, object]) -> dict[str, object]:
                raise RuntimeError("recheck failed")

        adapter = ScopedBackendAdapter(FailingBackend(), Scope())
        with self.assertRaisesRegex(RuntimeError, "recheck failed"):
            adapter.verify_rollback_context({})

    def test_invalid_scope_is_rejected_before_engine_binding(self) -> None:
        with self.assertRaisesRegex(TypeError, "scope"):
            ScopedBackendAdapter(Backend(), object())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
