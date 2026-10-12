# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Regression evidence for the bounded local issue-series state model."""

from tools.issue_series_model import MEMBERS, ModelState, check_bounded_model, step


def test_bounded_issue_series_model_explores_hostile_and_live_paths() -> None:
    evidence = check_bounded_model()
    assert evidence["states"] > 0
    assert evidence["accepted"] > 0
    assert evidence["rejected"] > 0


def test_model_rejects_same_identity_and_overlapping_member_commit() -> None:
    initial = ModelState(frozenset(), frozenset(), ("absent", "absent"))
    committed = step(step(initial, 0, "issue-a", "prepare"), 0, "issue-a", "commit")
    assert committed.claimed_ids == MEMBERS["issue-a"]
    try:
        step(committed, 1, "issue-a", "prepare")
    except ValueError as error:
        assert "duplicate" in str(error)
    else:
        raise AssertionError("same identity was prepared after a committed series")
