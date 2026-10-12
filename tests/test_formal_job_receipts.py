# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Deterministic lifecycle tests for candidate-bound formal-job receipts."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from typing import cast

from tools.formal_job_receipts import FormalJobError, FormalJobStore, tier_contract_digest


class FormalJobReceiptTests(unittest.TestCase):
    candidate_a = "a" * 40
    candidate_b = "b" * 40

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = FormalJobStore(Path(self.temporary.name) / "formal-jobs.sqlite3")
        self.digest = tier_contract_digest({"tier": "pr-publication", "workers": 2})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def start(self, candidate: str | None = None) -> dict[str, object]:
        return self.store.start(
            scope="coordinator",
            candidate=candidate or self.candidate_a,
            tier_digest=self.digest,
            resource_class="ordinary",
        )

    def test_lifecycle_receipt_is_candidate_and_tier_fenced(self) -> None:
        started = self.start()
        completed = self.store.execute(
            str(started["job_id"]),
            expected_revision=cast(int, started["revision"]),
            runner=lambda: (0, "tlc-passed"),
        )
        self.assertEqual("completed", completed["state"])
        receipt = self.store.receipt(
            str(started["job_id"]), candidate=self.candidate_a, tier_digest=self.digest
        )
        self.assertEqual(self.candidate_a, receipt["candidate"])
        with self.assertRaisesRegex(FormalJobError, "another candidate"):
            self.store.receipt(
                str(started["job_id"]), candidate=self.candidate_b, tier_digest=self.digest
            )

    def test_runner_executes_without_holding_receipt_authority(self) -> None:
        started = self.start()
        entered, allow_finish = threading.Event(), threading.Event()
        result: dict[str, object] = {}

        def runner() -> tuple[int, str]:
            entered.set()
            self.assertEqual("running", self.store.status(str(started["job_id"]))["state"])
            allow_finish.wait(timeout=2)
            return 0, "done"

        def execute() -> None:
            try:
                result.update(
                    self.store.execute(
                        str(started["job_id"]),
                        expected_revision=cast(int, started["revision"]),
                        runner=runner,
                    )
                )
            except FormalJobError as error:
                result["error"] = str(error)

        thread = threading.Thread(target=execute)
        thread.start()
        self.assertTrue(entered.wait(timeout=2))
        replacement = self.start(self.candidate_b)
        self.assertEqual("started", replacement["state"])
        allow_finish.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual("superseded", self.store.status(str(started["job_id"]))["state"])
        self.assertEqual("stale or fenced formal-job completion", result["error"])

    def test_recovery_fences_restarted_worker_and_stale_completion(self) -> None:
        started = self.start()
        running = self.store.begin(
            str(started["job_id"]), expected_revision=cast(int, started["revision"])
        )
        recovered = self.store.recover(
            str(started["job_id"]), expected_revision=int(running["revision"])
        )
        with self.assertRaisesRegex(FormalJobError, "stale or fenced"):
            self.store.finish(
                str(started["job_id"]),
                expected_revision=int(running["revision"]),
                execution_token=str(running["execution_token"]),
                state="completed",
                exit_code=0,
                detail="late",
            )
        rerun = self.store.begin(
            str(started["job_id"]), expected_revision=int(recovered["revision"])
        )
        finished = self.store.finish(
            str(started["job_id"]),
            expected_revision=int(rerun["revision"]),
            execution_token=str(rerun["execution_token"]),
            state="cancelled",
            exit_code=130,
            detail="operator",
        )
        self.assertEqual("cancelled", finished["state"])
        with self.assertRaisesRegex(FormalJobError, "incomplete, stale, failed"):
            self.store.receipt(
                str(started["job_id"]), candidate=self.candidate_a, tier_digest=self.digest
            )

    def test_concurrent_same_candidate_start_is_idempotent(self) -> None:
        barrier = threading.Barrier(3)
        jobs: list[dict[str, object]] = []

        def start() -> None:
            barrier.wait()
            jobs.append(self.start())

        workers = [threading.Thread(target=start) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=2)
        self.assertEqual(1, len({str(job["job_id"]) for job in jobs}))

    def test_invalid_resource_and_digest_fail_closed(self) -> None:
        with self.assertRaisesRegex(FormalJobError, "resource"):
            self.store.start(
                scope="x",
                candidate=self.candidate_a,
                tier_digest=self.digest,
                resource_class="unknown",
            )
        with self.assertRaisesRegex(FormalJobError, "digest"):
            self.store.start(
                scope="x", candidate=self.candidate_a, tier_digest="bad", resource_class="ordinary"
            )
