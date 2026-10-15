# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Bounded lock-timeout coverage split from the size-capped handoff suite."""

import multiprocessing
import time

from test_handoffctl import CORE, HandoffTest, hold_lock


class HandoffLockTimeoutTest(HandoffTest):
    """Keep lock timeout coverage under the source-size boundary."""

    def test_lock_timeout_is_bounded_and_does_not_wait_for_convoy(self) -> None:
        ready = multiprocessing.Event()
        release = multiprocessing.Event()
        process = multiprocessing.Process(target=hold_lock, args=(str(CORE.LOCK), ready, release))
        process.start()
        self.assertTrue(ready.wait(5))
        started = time.monotonic()
        try:
            with (
                self.assertRaisesRegex(CORE.LockTimeoutError, "LOCK_TIMEOUT"),
                CORE.locked(timeout=0.1),
            ):
                pass
        finally:
            release.set()
            process.join(5)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(0, process.exitcode)
