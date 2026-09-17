# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Recovery, mapping, duplicate-save, and privacy-boundary tests."""

import unittest

from tools.safe_exit import ExitError, ExitEvent, ExitSnapshot, FutureDiscussion, SafeExitJournal


def digest(char: str) -> str:
    return "sha256:" + char * 64


def snapshot(mapped: bool = False) -> ExitSnapshot:
    return ExitSnapshot(
        "exit/snapshot-1",
        digest("a"),
        ("proposal/1",),
        ("selection/1",),
        ("solution/1",),
        ("unresolved/1",),
        (FutureDiscussion("future/1", digest("b"), "AR-0029" if mapped else None),),
    )


class SafeExitTests(unittest.TestCase):
    def test_checkpoint_resume_mapping_and_atomic_commit(self) -> None:
        journal = SafeExitJournal("AR-0028", 1)
        journal.apply(ExitEvent("checkpoint-1", "AR-0028", 1, "checkpoint", snapshot()))
        journal.apply(ExitEvent("resume-1", "AR-0028", 2, "resume", checkpoint_digest=digest("a")))
        journal.apply(ExitEvent("map-1", "AR-0028", 3, "map_future", snapshot(mapped=True)))
        journal.apply(ExitEvent("commit-1", "AR-0028", 4, "commit_exit"))
        self.assertTrue(journal.committed)
        self.assertFalse(journal.pending)

    def test_duplicate_save_stale_resume_and_dropped_future_request_reject(self) -> None:
        journal = SafeExitJournal("AR-0028", 1)
        checkpoint = ExitEvent("checkpoint-1", "AR-0028", 1, "checkpoint", snapshot())
        journal.apply(checkpoint)
        with self.assertRaisesRegex(ExitError, "duplicate"):
            journal.apply(checkpoint)
        with self.assertRaisesRegex(ExitError, "stale"):
            journal.apply(
                ExitEvent("resume-stale", "AR-0028", 1, "resume", checkpoint_digest=digest("a"))
            )
        with self.assertRaisesRegex(ExitError, "unmapped"):
            journal.apply(ExitEvent("commit-1", "AR-0028", 2, "commit_exit"))

    def test_private_transcript_and_wrong_mapping_are_rejected(self) -> None:
        with self.assertRaisesRegex(ExitError, "public-safe"):
            ExitSnapshot("/private/transcript", digest("a"), (), (), (), (), ())
        journal = SafeExitJournal("AR-0028", 1)
        journal.apply(ExitEvent("checkpoint-1", "AR-0028", 1, "checkpoint", snapshot()))
        with self.assertRaisesRegex(ExitError, "not in checkpoint"):
            journal.apply(
                ExitEvent(
                    "map-wrong",
                    "AR-0028",
                    2,
                    "map_future",
                    snapshot=ExitSnapshot(
                        "exit/other",
                        digest("c"),
                        (),
                        (),
                        (),
                        (),
                        (FutureDiscussion("future/other", digest("d"), "AR-0030"),),
                    ),
                )
            )


if __name__ == "__main__":
    unittest.main()
