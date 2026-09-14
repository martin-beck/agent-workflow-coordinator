# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the uncalled caller-owned admission session seam."""

from __future__ import annotations

import json
import multiprocessing
import time
import unittest
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from tools.admission_lease import AdmissionLease, AdmissionLeaseError, validate_recheck
from tools.admission_session import AdmissionSession
from tools.upgrade_authority import AuthorityError


class _Scope:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.ordered = True

    def assert_ordered(self) -> None:
        self.events.append("order")
        if not self.ordered:
            raise AdmissionLeaseError("lock order is invalid")

    @contextmanager
    def hold(self) -> Iterator[object]:
        self.events.append("hold")
        try:
            yield None
        finally:
            self.events.append("release")


class _Store:
    def cas(self, expected_revision: int, record: Mapping[str, object]) -> dict[str, object]:
        return {**record, "revision": expected_revision + 1}


def _contended_session_worker(
    lock: Any,
    start: Any,
    events: Any,
    start_times: Any,
    end_times: Any,
    worker_id: int,
) -> None:
    class ProcessScope:
        def assert_ordered(self) -> None:
            events.put((worker_id, "ordered"))

        @contextmanager
        def hold(self) -> Iterator[object]:
            lock.acquire()
            events.put((worker_id, "held"))
            try:
                yield None
            finally:
                events.put((worker_id, "released"))
                lock.release()

    class ProcessStore:
        def cas(self, expected_revision: int, record: Mapping[str, object]) -> dict[str, object]:
            start_times[worker_id] = time.monotonic_ns()
            time.sleep(0.05)
            end_times[worker_id] = time.monotonic_ns()
            return {**record, "revision": expected_revision + 1}

    lease = AdmissionLease("project", "authority", f"fence-{worker_id}", "owner", "barrier", 1)
    recheck = validate_recheck(
        lease,
        project_id="project",
        authority_revision="authority",
        fencing_token=f"fence-{worker_id}",
        fencing_owner="owner",
        durable_barrier_id="barrier",
        revision=1,
    )
    session = AdmissionSession(lease, recheck, ProcessScope())
    start.wait()
    session.cas(
        ProcessStore(),
        1,
        {
            "project_id": "project",
            "authority_revision": "authority",
            "fencing_token": f"fence-{worker_id}",
            "fencing_owner": "owner",
            "durable_barrier_id": "barrier",
        },
    )


class AdmissionSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lease = AdmissionLease("project", "authority", "fence", "owner", "barrier", 1)
        self.recheck = validate_recheck(
            self.lease,
            project_id="project",
            authority_revision="authority",
            fencing_token="fence",  # noqa: S106
            fencing_owner="owner",
            durable_barrier_id="barrier",
            revision=1,
        )
        self.record = {
            "project_id": "project",
            "authority_revision": "authority",
            "fencing_token": "fence",
            "fencing_owner": "owner",
            "durable_barrier_id": "barrier",
        }

    def test_session_composes_control_and_selector_with_one_scope(self) -> None:
        scope = _Scope()
        session = AdmissionSession(self.lease, self.recheck, scope)
        result = session.cas(_Store(), 1, self.record)
        self.assertEqual(2, result["revision"])
        with TemporaryDirectory() as directory:
            path = Path(directory) / "selector.json"
            session.publish_selector(path, "new", "old")
            selector = json.loads(path.read_text())
            self.assertEqual("new", selector["active_release"])
            self.assertEqual("old", selector["previous_release"])
        self.assertEqual(
            ["order", "hold", "release", "order", "hold", "release"],
            scope.events,
        )

    def test_stale_recheck_is_rejected_before_scope_or_store(self) -> None:
        other = AdmissionLease("project", "authority", "other", "owner", "barrier", 2)
        stale = validate_recheck(
            other,
            project_id="project",
            authority_revision="authority",
            fencing_token="other",  # noqa: S106
            fencing_owner="owner",
            durable_barrier_id="barrier",
            revision=2,
        )
        scope = _Scope()
        with self.assertRaisesRegex(AdmissionLeaseError, "does not match"):
            AdmissionSession(self.lease, stale, scope)
        self.assertEqual([], scope.events)

    def test_failed_write_releases_scope(self) -> None:
        scope = _Scope()
        session = AdmissionSession(self.lease, self.recheck, scope)

        class FailingStore:
            def cas(self, _expected: int, _record: Mapping[str, object]) -> dict[str, object]:
                raise RuntimeError("injected write failure")

        with self.assertRaisesRegex(RuntimeError, "injected"):
            session.cas(FailingStore(), 1, self.record)
        self.assertEqual(["order", "hold", "release"], scope.events)

    def test_lock_order_failure_never_enters_scope(self) -> None:
        scope = _Scope()
        scope.ordered = False
        session = AdmissionSession(self.lease, self.recheck, scope)
        with self.assertRaisesRegex(AdmissionLeaseError, "lock order"):
            session.cas(_Store(), 1, self.record)
        self.assertEqual(["order"], scope.events)

    def test_failed_selector_publication_releases_scope(self) -> None:
        scope = _Scope()
        session = AdmissionSession(self.lease, self.recheck, scope)
        with TemporaryDirectory() as directory, self.assertRaises(AuthorityError):
            session.publish_selector(Path(directory), "new", "old")
        self.assertEqual(["order", "hold", "release"], scope.events)

    def test_two_process_writers_never_overlap_inside_scope(self) -> None:
        context = multiprocessing.get_context("fork")
        lock = context.Lock()
        start = context.Event()
        events = context.Queue()
        start_times = context.Array("q", [0, 0, 0], lock=False)
        end_times = context.Array("q", [0, 0, 0], lock=False)
        workers = [
            context.Process(
                target=_contended_session_worker,
                args=(lock, start, events, start_times, end_times, index),
            )
            for index in (1, 2)
        ]
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(5)
            self.assertEqual(0, worker.exitcode)
        self.assertGreater(start_times[1], 0)
        self.assertGreater(start_times[2], 0)
        self.assertGreater(end_times[1], 0)
        self.assertGreater(end_times[2], 0)
        self.assertTrue(
            end_times[1] <= start_times[2] or end_times[2] <= start_times[1],
            (list(start_times), list(end_times)),
        )
        self.assertEqual(6, events.qsize())


if __name__ == "__main__":
    unittest.main()
