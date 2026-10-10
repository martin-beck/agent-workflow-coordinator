# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Focused regression coverage for expired-claim process identity."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).with_name("test_handoffctl.py")
SPEC = importlib.util.spec_from_file_location("handoffctl_recovery_base", SOURCE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load handoffctl recovery test base")
BASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BASE)


class HandoffRecoveryTest(unittest.TestCase):
    setUp = BASE.HandoffTest.setUp
    tearDown = BASE.HandoffTest.tearDown
    make_task = BASE.HandoffTest.make_task
    refresh_views = BASE.HandoffTest.refresh_views

    def test_claim_process_disappearing_during_stat_read_is_absent(self) -> None:
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.read_text", side_effect=FileNotFoundError),
        ):
            self.assertIsNone(BASE.CORE.process_start_time(12345))

    def test_claim_process_inspection_failure_is_write_closed(self) -> None:
        with (
            patch("pathlib.Path.read_text", side_effect=PermissionError),
            self.assertRaisesRegex(RuntimeError, "cannot independently read"),
        ):
            BASE.CORE.process_start_time(12345)

    def test_snapshotless_recovery_rejects_other_execution_scope(self) -> None:
        self.make_task(
            status="in_progress",
            owner="worker-a",
            claim_expires="2000-01-01T00:00:00+00:00",
            claim_process={
                "pid": 99999999,
                "start_time": 0,
                "boot_digest": "sha256:" + "0" * 64,
                "pid_namespace": "1:1",
            },
        )
        with (
            patch.object(BASE.CORE, "commit", return_value=True),
            self.assertRaisesRegex(RuntimeError, "cannot verify the claim execution scope"),
        ):
            BASE.CORE.mutate(
                BASE.argparse.Namespace(task="AR-0001", expected_revision=1, note="Recover."),
                "recover-expired",
            )
