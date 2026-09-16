# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Executable mapping from lifecycle traces to TLA action outcomes."""

import unittest

from tools.formal_correspondence import INVARIANTS, validate_trace


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

    def test_required_invariant_names_are_explicit(self) -> None:
        self.assertEqual(
            ("NoReplacementBeforeBackup", "AmbiguousIsWriteClosed", "ReconcileRequiresFence"),
            INVARIANTS,
        )
