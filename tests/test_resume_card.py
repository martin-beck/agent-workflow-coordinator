# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Contract coverage for deterministic, read-only drift-aware resume cards."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import assumption_revalidation, handoffctl, resume_card, retry_circuit


def task(**changes: object) -> dict[str, object]:
    return {
        "id": "AR-0001",
        "title": "Resume task",
        "summary": "Resume from durable records.",
        "priority": "P1",
        "status": "in_progress",
        "next_action": "Run the focused verification.",
        "task_revision": 4,
        "observed_branch": "feature/resume",
        "observed_head": "a" * 40,
        "checkpoint_commit": "",
        **changes,
    }


class ResumeCardTests(unittest.TestCase):
    def test_card_is_stable_and_selects_one_material_assumption_action(self) -> None:
        meta = task()
        record = assumption_revalidation._record(
            "declare",
            "base",
            "base",
            True,
            {"head": "a" * 40},
            2,
            "",
        )
        meta["assumption_revalidation"] = [record]
        with patch.object(
            assumption_revalidation, "current_binding", return_value={"head": "b" * 40}
        ):
            first = resume_card.build(Path("/state"), Path("/projects"), meta, [meta])
            second = resume_card.build(Path("/state"), Path("/projects"), meta, [meta])
        self.assertEqual(first, second)
        self.assertEqual(
            [{"field": "assumptions.base", "kind": "base", "material": True, "status": "stale"}],
            first["drift"],
        )
        self.assertEqual("revalidate-material-assumption", first["next_action"]["kind"])
        self.assertEqual([], first["acknowledgements"])
        self.assertEqual(1, len([first["next_action"]]))
        self.assertIn("NEXT_ACTION=", resume_card.render_text(first))

    def test_nonmaterial_drift_remains_visible_without_an_acknowledgement_action(self) -> None:
        meta = task()
        record = assumption_revalidation._record(
            "declare",
            "cache",
            "observation",
            False,
            {"ref": "scan.json", "digest": "sha256:" + "a" * 64},
            2,
            "",
        )
        meta["assumption_revalidation"] = [record]
        with patch.object(
            assumption_revalidation,
            "current_binding",
            return_value={"ref": "scan.json", "digest": "sha256:" + "b" * 64},
        ):
            card = resume_card.build(Path("/state"), Path("/projects"), meta, [meta])
        self.assertEqual("task-next-action", card["next_action"]["kind"])
        self.assertEqual(False, card["drift"][0]["material"])
        self.assertEqual([], card["acknowledgements"])

    def test_acknowledgements_include_only_material_revalidation_records(self) -> None:
        meta = task()
        declaration = assumption_revalidation._record(
            "declare", "base", "base", True, {"head": "a" * 40}, 2, ""
        )
        material = assumption_revalidation._record(
            "revalidate", "base", "base", True, {"head": "b" * 40}, 3, declaration["digest"]
        )
        cache_declaration = assumption_revalidation._record(
            "declare",
            "cache",
            "observation",
            False,
            {"ref": "scan.json", "digest": "sha256:" + "b" * 64},
            4,
            material["digest"],
        )
        harmless = assumption_revalidation._record(
            "revalidate",
            "cache",
            "observation",
            False,
            {"ref": "scan.json", "digest": "sha256:" + "c" * 64},
            5,
            cache_declaration["digest"],
        )
        meta["assumption_revalidation"] = [declaration, material, cache_declaration, harmless]
        with patch.object(
            assumption_revalidation, "current_binding", side_effect=lambda *_: {"head": "b" * 40}
        ):
            card = resume_card.build(Path("/state"), Path("/projects"), meta, [meta])
        self.assertEqual(
            [{"field": "assumptions.base", "kind": "base", "record": "revalidate"}],
            card["acknowledgements"],
        )

    def test_retry_recovery_precedes_material_drift(self) -> None:
        meta = task()
        identity = "sha256:" + "c" * 64
        retry_circuit.record_failure(meta, identity, "EXIT", 3, "2026-10-13T00:00:00+00:00")
        meta["task_revision"] = 5
        retry_circuit.record_failure(meta, identity, "EXIT", 3, "2026-10-13T00:01:00+00:00")
        meta["task_revision"] = 6
        retry_circuit.record_failure(meta, identity, "EXIT", 3, "2026-10-13T00:02:00+00:00")
        card = resume_card.build(Path("/state"), Path("/projects"), meta, [meta])
        self.assertEqual("held", card["retry"]["state"])
        self.assertEqual("recover-retry-circuit", card["next_action"]["kind"])

    def test_malformed_durable_record_fails_closed(self) -> None:
        meta = task(assumption_revalidation=[{}])
        with self.assertRaisesRegex(ValueError, "fields are not exact"):
            resume_card.build(Path("/state"), Path("/projects"), meta, [meta])

    def test_cli_text_and_json_share_one_read_only_card(self) -> None:
        meta = task()
        card = resume_card.build(Path("/state"), Path("/projects"), meta, [meta])
        stream = io.StringIO()
        with (
            patch.object(handoffctl, "all_tasks", return_value=[(Path("task"), meta, "")]),
            patch.object(handoffctl, "config", return_value={"projects_root": "/projects"}),
            patch.object(handoffctl, "locked", return_value=contextlib.nullcontext()),
            patch.object(resume_card, "build", return_value=card),
            patch("sys.stdout", stream),
        ):
            self.assertEqual(
                0,
                handoffctl.dispatch_read_only_command(
                    argparse.Namespace(cmd="resume-card", task="AR-0001", json=True)
                ),
            )
        self.assertEqual(card, json.loads(stream.getvalue()))

        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="cmd", required=True)
        resume_card.add_command_parser(commands)
        self.assertEqual("AR-0001", parser.parse_args(["resume-card", "AR-0001", "--json"]).task)
