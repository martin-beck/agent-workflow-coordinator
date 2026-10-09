# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Safety checks for the bounded Git command latency benchmark."""

import hashlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from git_command_latency_benchmark import (
    case_order,
    classify,
    digest_stream,
    percentile,
    product_input_digest,
)


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

    def test_output_digest_includes_all_bytes(self) -> None:
        payload = b"result\n" * 10000
        self.assertEqual(
            (hashlib.sha256(payload).hexdigest(), len(payload)), digest_stream(io.BytesIO(payload))
        )

    def test_product_fingerprint_detects_dirty_checkout_without_head_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)  # noqa: S603,S607
            source = root / "README.md"
            source.write_text("before\n")
            subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)  # noqa: S603,S607
            subprocess.run(  # noqa: S603
                [
                    "/usr/bin/git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-q",
                    "-m",
                    "fixture",
                ],
                check=True,
            )
            before = product_input_digest(root)
            source.write_text("after\n")
            self.assertNotEqual(before, product_input_digest(root))


if __name__ == "__main__":
    unittest.main()
