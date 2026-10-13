import argparse
import sys
import unittest

sys.path.insert(0, "/srv/data/projects/agent-workflow-coordinator-ar0034-legacy-binding-repair-v075/tools")
import handoffctl as h


class LegacyBindingRepairTests(unittest.TestCase):
    def setUp(self):
        self.args = argparse.Namespace(expected_revision=4, worktree_key="legacy-key", note="verified stale")
        self.meta = {"task_revision": 4, "status": "open", "owner": "", "claim_expires": "", "branch": "", "worktree_key": "legacy-key"}

    def test_exact_match_clears_only_key(self):
        h.apply_repair_legacy_binding(self.args, self.meta, [])
        self.assertEqual("", self.meta["worktree_key"])
        self.assertEqual("open", self.meta["status"])

    def test_hostile_inputs_rejected(self):
        for replacement, message in (({"expected_revision": 3}, "stale revision"), ({"worktree_key": "other"}, "does not match"), ({"worktree_key": "../escape"}, "invalid"), ({"note": ""}, "note")):
            value = dict(self.meta)
            value.update({"worktree_key": "legacy-key"})
            args = argparse.Namespace(**{**vars(self.args), **replacement})
            with self.assertRaisesRegex(RuntimeError, message):
                h.apply_repair_legacy_binding(args, value, [])

    def test_state_preconditions_rejected(self):
        for replacement, message in (({"branch": "repair/old"}, "empty legacy branch"), ({"status": "in_progress", "owner": "worker", "claim_expires": "future"}, "open, unowned")):
            value = dict(self.meta)
            value.update(replacement)
            with self.assertRaisesRegex(RuntimeError, message):
                h.apply_repair_legacy_binding(self.args, value, [])


if __name__ == "__main__":
    unittest.main()
