# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Unit checks for bounded mixed-route benchmark aggregation."""

import json
import tempfile
import unittest
from pathlib import Path
from typing import cast

from git_mixed_route_benchmark import lock_aggregates, route_argv


class MixedRouteBenchmarkTests(unittest.TestCase):
    def test_every_required_route_uses_handoffctl_arguments(self) -> None:
        for route in (
            "snapshot",
            "doctor",
            "claim",
            "heartbeat",
            "update",
            "run",
            "release",
            "reconcile",
        ):
            with self.subTest(route=route):
                self.assertEqual(route, route_argv(route, 1)[0])

    def test_queue_excludes_the_new_holder_itself(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            trace = Path(temp) / "trace.jsonl"
            trace.write_text(
                json.dumps(
                    {
                        "phase": "git_mutate",
                        "wait_ms": 10.0,
                        "hold_ms": 100.0,
                        "ended_ns": 1_000_000_000,
                    }
                )
                + "\n"
            )
            result = lock_aggregates([trace])
        self.assertEqual(0, result["max_wait_queue_depth"])
        self.assertEqual(0.0, result["wait_queue_depth_p95"])
        phases = cast(dict[str, dict[str, object]], result["phase_ms"])
        self.assertEqual(10.0, phases["git_mutate"]["wait_max"])

    def test_queue_counts_waiters_while_another_worker_holds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            trace = Path(temp) / "trace.jsonl"
            trace.write_text(
                "\n".join(
                    json.dumps(item)
                    for item in (
                        {
                            "phase": "git_mutate",
                            "wait_ms": 0.0,
                            "hold_ms": 100.0,
                            "ended_ns": 1_000_000_000,
                        },
                        {
                            "phase": "git_mutate",
                            "wait_ms": 50.0,
                            "hold_ms": 50.0,
                            "ended_ns": 1_050_000_000,
                        },
                    )
                )
                + "\n"
            )
            result = lock_aggregates([trace])
        self.assertEqual(1, result["max_wait_queue_depth"])
        self.assertEqual(1.0, result["wait_queue_depth_p95"])

    def test_zero_wait_shared_readers_never_create_negative_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            trace = Path(temp) / "trace.jsonl"
            trace.write_text(
                "\n".join(
                    json.dumps(item)
                    for item in (
                        {
                            "phase": "doctor",
                            "wait_ms": 0.0,
                            "hold_ms": 100.0,
                            "ended_ns": 1_000_000_000,
                        },
                        {
                            "phase": "snapshot",
                            "wait_ms": 0.0,
                            "hold_ms": 50.0,
                            "ended_ns": 980_000_000,
                        },
                    )
                )
                + "\n"
            )
            result = lock_aggregates([trace])
        self.assertEqual(0, result["max_wait_queue_depth"])
        self.assertEqual(0.0, result["wait_queue_depth_p50"])


if __name__ == "__main__":
    unittest.main()
