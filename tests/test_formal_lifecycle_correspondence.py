# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Executable mapping from lifecycle traces to TLA action outcomes."""

import unittest
from pathlib import Path
from typing import cast

from tools.formal_correspondence import (
    INVARIANT_PREDICATES,
    formal_provenance,
    validate_runtime_trace,
    validate_trace,
)

ROOT = Path(__file__).resolve().parents[1]


class FormalLifecycleCorrespondenceTests(unittest.TestCase):
    def test_success_and_failure_traces_map_to_named_actions(self) -> None:
        self.assertEqual(("ExecuteSuccess",), validate_trace(("backup_verified",)))
        self.assertEqual(
            ("ExecuteReject", "ExecuteSuccess"),
            validate_trace(("publication_failed", "retry_verified")),
        )

    def test_invalid_nonterminal_and_unknown_traces_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            validate_trace(("publication_failed", "backup_verified"))
        with self.assertRaises(ValueError):
            validate_trace(("unknown",))

    def test_runtime_state_invariants_are_evaluated(self) -> None:
        self.assertEqual(
            ("ExecuteReject", "ExecuteSuccess"),
            validate_runtime_trace(
                (
                    {
                        "event": "publication_failed",
                        "revision_before": 7,
                        "revision_after": 7,
                        "lock_held": True,
                    },
                    {"event": "retry_verified", "revision_before": 7, "revision_after": 7},
                )
            ),
        )
        with self.assertRaisesRegex(ValueError, "ProjectionAtomicity"):
            validate_runtime_trace(
                (
                    {
                        "event": "restore_failed",
                        "revision_before": 1,
                        "revision_after": 2,
                        "lock_held": True,
                    },
                )
            )

    def test_required_invariant_names_are_explicit(self) -> None:
        self.assertEqual(
            {
                "NoReplacementBeforeBackup": "ProjectionAtomicity",
                "AmbiguousIsWriteClosed": "LockSafety",
                "ReconcileRequiresFence": "RevisionAccounting",
            },
            INVARIANT_PREDICATES,
        )

    def test_provenance_binds_authoritative_model_and_config(self) -> None:
        provenance = formal_provenance(ROOT)
        self.assertEqual(64, len(cast(str, provenance["model_sha256"])))
        self.assertEqual(64, len(cast(str, provenance["config_sha256"])))
        self.assertEqual(INVARIANT_PREDICATES, provenance["invariants"])

    def test_runtime_invariant_failures_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "LockSafety"):
            validate_runtime_trace(
                (
                    {
                        "event": "restore_failed",
                        "revision_before": 1,
                        "revision_after": 1,
                        "lock_held": False,
                    },
                )
            )
        with self.assertRaisesRegex(ValueError, "ReconcileRequiresFence"):
            validate_runtime_trace(
                (
                    {"event": "reconcile", "fenced": False},
                    {"event": "retry_verified"},
                )
            )
