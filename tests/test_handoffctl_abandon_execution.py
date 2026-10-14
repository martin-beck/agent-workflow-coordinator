# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Focused state-root orphan-execution recovery coverage."""

from __future__ import annotations

import argparse
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).with_name("test_handoffctl.py")
SPEC = importlib.util.spec_from_file_location("handoffctl_abandon_execution_base", SOURCE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load handoffctl test base")
BASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BASE)


def reservation() -> dict[str, object]:
    return {
        "execution_id": "a" * 32,
        "owner": "worker-a",
        "task_revision": 1,
        "envelope_digest": "sha256:" + "b" * 64,
        "command_class": "test",
        "scope_digest": "sha256:" + "c" * 64,
        "argv_digest": "d" * 64,
        "expires_at": "2000-01-01T00:00:00+00:00",
    }


class HandoffAbandonExecutionTest(unittest.TestCase):
    setUp = BASE.HandoffTest.setUp
    tearDown = BASE.HandoffTest.tearDown
    make_task = BASE.HandoffTest.make_task
    refresh_views = BASE.HandoffTest.refresh_views

    def args(self, **changes: object) -> argparse.Namespace:
        values = {
            "task": "AR-0001",
            "expected_revision": 1,
            "execution_id": "a" * 32,
            "evidence_ref": "evidence/AR-0113-abandonment.json",
            "evidence_digest": "sha256:" + "e" * 64,
            "note": "Verified the former worker is absent and no result was retained.",
        }
        values.update(changes)
        return argparse.Namespace(**values)

    def test_abandonment_clears_only_expired_unknown_reservation_with_audit(self) -> None:
        path = self.make_task(
            status="in_progress",
            owner="worker-a",
            claim_expires="2000-01-01T00:00:00+00:00",
            execution_reservation=reservation(),
        )
        with (
            patch.object(BASE.CORE, "require_claimed_worker_absent"),
            patch.object(BASE.CORE, "commit", return_value=True),
            patch.object(BASE.CORE, "now", return_value="2026-10-14T00:00:00+00:00"),
        ):
            BASE.CORE.mutate(self.args(), "abandon-execution")
        recovered, body = BASE.CORE.read_task(path)
        self.assertNotIn("execution_reservation", recovered)
        self.assertEqual("a" * 32, recovered["execution_abandonments"][0]["execution_id"])
        self.assertEqual("open", recovered["status"])
        self.assertEqual("", recovered["owner"])
        self.assertIn("Abandoned expired execution", body)

    def test_abandonment_rejects_live_mismatch_and_completed_reservations(self) -> None:
        meta: dict[str, object] = {"task_revision": 1, "execution_reservation": reservation()}
        with self.assertRaisesRegex(RuntimeError, "identity does not match"):
            BASE.CORE.apply_abandon_execution(self.args(execution_id="f" * 32), meta)
        live = reservation()
        live["expires_at"] = "2999-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(RuntimeError, "EXECUTION_IN_PROGRESS"):
            BASE.CORE.apply_abandon_execution(
                self.args(), {"task_revision": 1, "execution_reservation": live}
            )
        result = {
            "execution_id": "a" * 32,
            "owner": "worker-a",
            "envelope_digest": "sha256:" + "b" * 64,
            "argv_digest": "d" * 64,
            "classification": "EXIT",
            "returncode": 0,
            "recorded_at": "2026-10-13T00:00:00+00:00",
        }
        with (
            patch.object(BASE.CORE, "require_claimed_worker_absent"),
            self.assertRaisesRegex(RuntimeError, "durable result"),
        ):
            BASE.CORE.apply_abandon_execution(
                self.args(),
                {
                    "task_revision": 1,
                    "execution_reservation": reservation(),
                    "execution_result": result,
                },
            )
