# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Negative-path tests for correctness-first upgrade admission."""

from __future__ import annotations

import unittest

from tools.upgrade_admission import (
    PREFLIGHT_PREDICATES,
    QUIESCENCE_PREDICATES,
    REOPEN_PREDICATES,
    AdmissionError,
    admit_preflight,
    admit_quiesced,
    admit_reopen,
)


def complete(names: tuple[str, ...]) -> dict[str, bool]:
    return dict.fromkeys(names, True)


class UpgradeAdmissionTests(unittest.TestCase):
    def test_preflight_requires_every_prerequisite(self) -> None:
        snapshot = complete(PREFLIGHT_PREDICATES)
        admit_preflight(snapshot)
        for predicate in PREFLIGHT_PREDICATES:
            denied = dict(snapshot)
            denied[predicate] = False
            with self.subTest(predicate=predicate), self.assertRaises(AdmissionError):
                admit_preflight(denied)

    def test_quiescence_requires_barrier_and_drained_work(self) -> None:
        snapshot = complete(QUIESCENCE_PREDICATES)
        admit_quiesced(snapshot)
        for predicate in QUIESCENCE_PREDICATES:
            denied = dict(snapshot)
            denied.pop(predicate)
            with self.subTest(predicate=predicate), self.assertRaises(AdmissionError):
                admit_quiesced(denied)

    def test_reopen_requires_validation_and_fencing(self) -> None:
        snapshot = complete(REOPEN_PREDICATES)
        admit_reopen(snapshot)
        for predicate in REOPEN_PREDICATES:
            denied = dict(snapshot)
            denied[predicate] = False
            with self.subTest(predicate=predicate), self.assertRaises(AdmissionError):
                admit_reopen(denied)

    def test_non_boolean_truthy_values_fail_closed(self) -> None:
        snapshot = complete(PREFLIGHT_PREDICATES)
        snapshot["state_clean"] = 1
        with self.assertRaises(AdmissionError):
            admit_preflight(snapshot)


if __name__ == "__main__":
    unittest.main()
