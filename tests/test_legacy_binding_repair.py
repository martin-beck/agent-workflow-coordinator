# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

import argparse
import unittest

from tools import handoffctl as h


class LegacyBindingRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.args = argparse.Namespace(
            expected_revision=4, worktree_key="legacy-key", note="verified stale"
        )
        self.meta = {
            "task_revision": 4,
            "status": "open",
            "owner": "",
            "claim_expires": "",
            "branch": "",
            "worktree_key": "legacy-key",
        }

    def test_exact_match_clears_only_key(self) -> None:
        h.apply_repair_legacy_binding(self.args, self.meta, [])
        self.assertEqual("", self.meta["worktree_key"])
        self.assertEqual("open", self.meta["status"])

    def test_hostile_inputs_rejected(self) -> None:
        for replacement, message in (
            ({"expected_revision": 3}, "stale revision"),
            ({"worktree_key": "other"}, "does not match"),
            ({"worktree_key": "../escape"}, "does not match"),
            ({"note": ""}, "note"),
        ):
            value = dict(self.meta)
            value.update({"worktree_key": "legacy-key"})
            args = argparse.Namespace(**{**vars(self.args), **replacement})
            with self.assertRaisesRegex(RuntimeError, message):
                h.apply_repair_legacy_binding(args, value, [])

    def test_state_preconditions_rejected(self) -> None:
        for replacement, message in (
            ({"branch": "repair/old"}, "empty legacy branch"),
            (
                {"status": "in_progress", "owner": "worker", "claim_expires": "future"},
                "inactive, unowned",
            ),
        ):
            value = dict(self.meta)
            value.update(replacement)
            with self.assertRaisesRegex(RuntimeError, message):
                h.apply_repair_legacy_binding(self.args, value, [])

    def test_completed_history_can_clear_its_exact_stale_key(self) -> None:
        value = {**self.meta, "status": "done", "branch": "legacy/completed"}
        h.apply_repair_legacy_binding(self.args, value, [])
        self.assertEqual("", value["worktree_key"])
        self.assertEqual("legacy/completed", value["branch"])


if __name__ == "__main__":
    unittest.main()
