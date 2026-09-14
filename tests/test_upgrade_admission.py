# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Negative-path tests for correctness-first upgrade admission."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "upgrade_admission", ROOT / "tools/upgrade_admission.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
PREFLIGHT_PREDICATES = MODULE.PREFLIGHT_PREDICATES
QUIESCENCE_PREDICATES = MODULE.QUIESCENCE_PREDICATES
REOPEN_PREDICATES = MODULE.REOPEN_PREDICATES
AdmissionError = MODULE.AdmissionError
admit_preflight = MODULE.admit_preflight
admit_quiesced = MODULE.admit_quiesced
admit_reopen = MODULE.admit_reopen
admit_safe_mode = MODULE.admit_safe_mode
recheck_before_replacement = MODULE.recheck_before_replacement


def complete(names: tuple[str, ...]) -> dict[str, object]:
    snapshot: dict[str, object] = dict.fromkeys(names, True)
    snapshot.update(
        {
            "operation_id": "upgrade-001",
            "project_id": "11111111-1111-4111-8111-111111111111",
            "backend": "sqlite",
            "state_revision": 4,
            "fencing_token": "fence-4",
            "fencing_owner": "worker-1",
            "authority_revision": "authority-4",
            "barrier_identity_digest": "a" * 64,
            "envelope_digest": "b" * 64,
            "durable_barrier_id": "barrier-4",
            "target": "new",
        }
    )
    return snapshot


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
        missing_barrier = dict(snapshot)
        missing_barrier.pop("durable_barrier_id")
        with self.assertRaises(AdmissionError):
            admit_quiesced(missing_barrier)
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

    def test_unknown_and_stale_identity_fail_closed(self) -> None:
        snapshot = complete(PREFLIGHT_PREDICATES)
        snapshot["unexpected"] = True
        with self.assertRaises(AdmissionError):
            admit_preflight(snapshot)
        snapshot = complete(QUIESCENCE_PREDICATES)
        current = dict(snapshot)
        current["state_revision"] = 5
        with self.assertRaises(AdmissionError):
            recheck_before_replacement(snapshot, current)
        recheck_before_replacement(snapshot, dict(snapshot))
        for field, value in (
            ("operation_id", ""),
            ("operation_id", "upgrade:bad"),
            ("project_id", ""),
            ("project_id", "project-1"),
            ("project_id", "11111111-1111-4111-0111-111111111111"),
            ("backend", "unknown"),
            ("fencing_token", ""),
            ("fencing_owner", ""),
            ("authority_revision", ""),
            ("barrier_identity_digest", "not-a-digest"),
            ("envelope_digest", "not-a-digest"),
            ("state_revision", 0),
            ("state_revision", False),
        ):
            denied = complete(PREFLIGHT_PREDICATES)
            denied[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(AdmissionError):
                admit_preflight(denied)

    def test_reopen_target_and_safe_mode_failure_paths(self) -> None:
        snapshot = complete(REOPEN_PREDICATES)
        snapshot["target"] = "other"
        with self.assertRaises(AdmissionError):
            admit_reopen(snapshot)
        snapshot = complete(REOPEN_PREDICATES)
        snapshot.pop("durable_barrier_id")
        with self.assertRaises(AdmissionError):
            admit_reopen(snapshot)
        snapshot = complete(REOPEN_PREDICATES)
        snapshot["validation_failed"] = True
        with self.assertRaises(AdmissionError):
            admit_reopen(snapshot)
        for value in (1, 0, "true"):
            snapshot = complete(REOPEN_PREDICATES)
            snapshot["validation_failed"] = value
            with self.subTest(value=value), self.assertRaises(AdmissionError):
                admit_reopen(snapshot)
        snapshot = complete(REOPEN_PREDICATES)
        snapshot["safe_mode_ready"] = True
        admit_safe_mode(snapshot)
        snapshot["safe_mode_ready"] = False
        with self.assertRaises(AdmissionError):
            admit_safe_mode(snapshot)
        snapshot.pop("durable_barrier_id")
        with self.assertRaises(AdmissionError):
            admit_safe_mode(snapshot)
        snapshot["unexpected"] = True
        with self.assertRaises(AdmissionError):
            admit_safe_mode(snapshot)
        for value in (1, 0, "true"):
            snapshot = complete(REOPEN_PREDICATES)
            snapshot["safe_mode_ready"] = value
            with self.subTest(value=value), self.assertRaises(AdmissionError):
                admit_safe_mode(snapshot)

    def test_non_boolean_truthy_values_fail_closed(self) -> None:
        snapshot = complete(PREFLIGHT_PREDICATES)
        snapshot["state_clean"] = 1
        with self.assertRaises(AdmissionError):
            admit_preflight(snapshot)


if __name__ == "__main__":
    unittest.main()
