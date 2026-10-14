# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Contract coverage for execution-abandonment audit records."""

from __future__ import annotations

import argparse
import unittest

from tools import execution_abandonment


def reservation() -> dict[str, object]:
    return {
        "execution_id": "a" * 32,
        "owner": "worker-a",
        "task_revision": 7,
        "envelope_digest": "sha256:" + "b" * 64,
        "command_class": "test",
        "scope_digest": "sha256:" + "c" * 64,
        "argv_digest": "d" * 64,
        "expires_at": "2026-10-13T00:00:00+00:00",
    }


class ExecutionAbandonmentTest(unittest.TestCase):
    def test_build_binds_exact_reservation_and_local_evidence(self) -> None:
        record = execution_abandonment.build(
            reservation(),
            argparse.Namespace(
                execution_id="a" * 32,
                evidence_ref="evidence/AR-0113-abandonment.json",
                evidence_digest="sha256:" + "e" * 64,
            ),
            "2026-10-14T00:00:00+00:00",
        )
        self.assertTrue(execution_abandonment.valid(record))
        self.assertEqual(
            execution_abandonment.reservation_digest(reservation()), record["reservation_digest"]
        )

    def test_rejects_private_or_incomplete_evidence_and_duplicate_history(self) -> None:
        record = execution_abandonment.build(
            reservation(),
            argparse.Namespace(
                execution_id="a" * 32,
                evidence_ref="evidence/AR-0113-abandonment.json",
                evidence_digest="sha256:" + "e" * 64,
            ),
            "2026-10-14T00:00:00+00:00",
        )
        private = {**record, "evidence_ref": "/home/martin/private"}
        self.assertFalse(execution_abandonment.valid(private))
        self.assertFalse(execution_abandonment.valid_records([record, record]))
        self.assertFalse(execution_abandonment.valid_records([record] * 33))
