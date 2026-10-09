# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Safety checks for the bounded Git command latency benchmark."""

import unittest

from git_command_latency_benchmark import case_order, classify, percentile


class GitCommandLatencyBenchmarkTests(unittest.TestCase):
    def test_percentiles_use_bounded_nearest_rank(self) -> None:
        values = [9.0, 3.0, 1.0, 7.0, 5.0]
        self.assertEqual(5.0, percentile(values, 0.5))
        self.assertEqual(9.0, percentile(values, 0.95))
        self.assertEqual(9.0, percentile(values, 1))

    def test_cases_alternate_to_limit_warm_order_bias(self) -> None:
        self.assertEqual(("baseline", "candidate"), case_order(0))
        self.assertEqual(("candidate", "baseline"), case_order(1))

    def test_failures_are_not_counted_as_success(self) -> None:
        self.assertEqual("ok", classify(0, b""))
        self.assertEqual("lock_timeout", classify(1, b"LOCK_TIMEOUT"))
        self.assertEqual("stale_or_changed", classify(1, b"PROJECT_STATE.md is stale"))
        self.assertEqual("other_error", classify(2, b"failed to sign"))


if __name__ == "__main__":
    unittest.main()
