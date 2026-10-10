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
