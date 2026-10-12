# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Regression evidence for the bounded local issue-series state model."""

import unittest

from tools.issue_series_model import MEMBERS, ModelState, _invariant, check_bounded_model, step


class IssueSeriesModelTests(unittest.TestCase):
    def test_bounded_model_explores_hostile_and_live_paths(self) -> None:
        evidence = check_bounded_model()
        self.assertGreater(evidence["states"], 0)
        self.assertGreater(evidence["accepted"], 0)
        self.assertGreater(evidence["rejected"], 0)

    def test_model_rejects_same_identity_and_overlapping_member_commit(self) -> None:
        initial = ModelState(frozenset(), frozenset(), ("absent", "absent"))
        committed = step(step(initial, 0, "issue-a", "prepare"), 0, "issue-a", "commit")
        self.assertEqual(MEMBERS["issue-a"], committed.claimed_ids)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            step(committed, 1, "issue-a", "prepare")

    def test_model_rejects_invalid_inputs_and_detects_partial_authority(self) -> None:
        initial = ModelState(frozenset(), frozenset(), ("absent", "absent"))
        for creator, identity, action, message in (
            (2, "issue-a", "prepare", "unknown creator"),
            (0, "unknown", "prepare", "unknown creator"),
            (0, "issue-a", "unknown", "unknown issue-series action"),
        ):
            with (
                self.subTest(creator=creator, identity=identity, action=action),
                self.assertRaisesRegex(ValueError, message),
            ):
                step(initial, creator, identity, action)
        partial = ModelState(frozenset({"issue-a"}), frozenset(), ("committed", "absent"))
        with self.assertRaisesRegex(AssertionError, "partial issue-series members"):
            _invariant(partial)
