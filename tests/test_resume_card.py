# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Contract coverage for deterministic, read-only drift-aware resume cards."""

from __future__ import annotations

import argparse
import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools import assumption_revalidation, handoffctl, resume_card, retry_circuit


def task(**changes: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": "AR-0001",
        "title": "Resume task",
        "summary": "Resume from durable records.",
        "priority": "P1",
        "status": "in_progress",
        "next_action": "Run the focused verification.",
        "task_revision": 4,
        "updated_at": "2026-10-13T00:00:00+00:00",
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

    def test_execution_reservation_precedes_retry_drift_and_task_actions(self) -> None:
        meta = task(
            execution_reservation={
                "execution_id": "a" * 32,
                "owner": "worker",
                "task_revision": 4,
                "envelope_digest": "sha256:" + "b" * 64,
                "command_class": "test",
                "scope_digest": "sha256:" + "c" * 64,
                "argv_digest": "d" * 64,
                "expires_at": "2020-01-01T00:00:00+00:00",
            }
        )
        identity = "sha256:" + "c" * 64
        retry_circuit.record_failure(meta, identity, "EXIT", 3, "2026-10-13T00:00:00+00:00")
        meta["task_revision"] = 5
        retry_circuit.record_failure(meta, identity, "EXIT", 3, "2026-10-13T00:01:00+00:00")
        meta["task_revision"] = 6
        retry_circuit.record_failure(meta, identity, "EXIT", 3, "2026-10-13T00:02:00+00:00")
        card = resume_card.build(Path("/state"), Path("/projects"), meta, [meta])
        self.assertEqual("expired", card["recovery"]["execution"]["state"])
        self.assertEqual("recover-execution-reservation", card["next_action"]["kind"])

    def test_live_execution_reservation_requires_wait_before_task_action(self) -> None:
        meta = task(
            execution_reservation={
                "execution_id": "a" * 32,
                "owner": "worker",
                "task_revision": 4,
                "envelope_digest": "sha256:" + "b" * 64,
                "command_class": "test",
                "scope_digest": "sha256:" + "c" * 64,
                "argv_digest": "d" * 64,
                "expires_at": "2099-01-01T00:00:00+00:00",
            }
        )
        card = resume_card.build(Path("/state"), Path("/projects"), meta, [meta])
        self.assertEqual("live", card["recovery"]["execution"]["state"])
        self.assertEqual("wait-for-execution-reservation", card["next_action"]["kind"])

    def test_execution_expiry_is_determined_by_the_authority_snapshot(self) -> None:
        meta = task(
            execution_reservation={
                "execution_id": "a" * 32,
                "owner": "worker",
                "task_revision": 4,
                "envelope_digest": "sha256:" + "b" * 64,
                "command_class": "test",
                "scope_digest": "sha256:" + "c" * 64,
                "argv_digest": "d" * 64,
                "expires_at": "2026-10-13T01:00:00+00:00",
            },
            updated_at="2026-10-13T00:00:00+00:00",
        )

        class NoClockDatetime(resume_card.dt.datetime):
            @classmethod
            def now(cls, *_args: object, **_kwargs: object) -> object:
                raise AssertionError("wall clock")

        with patch.object(resume_card.dt, "datetime", NoClockDatetime):
            first = resume_card.build(Path("/state"), Path("/projects"), meta, [meta])
            second = resume_card.build(Path("/state"), Path("/projects"), meta, [meta])
        self.assertEqual(first, second)
        self.assertEqual("live", first["recovery"]["execution"]["state"])
        self.assertEqual("2026-10-13T00:00:00+00:00", first["recovery"]["execution"]["observed_at"])

    def test_malformed_durable_record_fails_closed(self) -> None:
        meta = task(assumption_revalidation=[{}])
        with self.assertRaisesRegex(ValueError, "fields are not exact"):
            resume_card.build(Path("/state"), Path("/projects"), meta, [meta])

    def test_unknown_evidence_metadata_fails_closed_without_projection(self) -> None:
        meta = task(spec_acceptance={"unexpected": "private value"})
        with self.assertRaisesRegex(ValueError, "spec acceptance is incomplete or unknown"):
            resume_card.build(Path("/state"), Path("/projects"), meta, [meta])

    def test_command_snapshot_rejects_unknown_task_metadata_before_projection(self) -> None:
        meta = task(unrecognized_durable_metadata="private value")
        errors = handoffctl.resume_card_snapshot_errors(
            [(Path("AR-0001.md"), meta, "")], handoffctl.DEFAULT_EVIDENCE_POLICY
        )
        self.assertIn("AR-0001.md: unknown field unrecognized_durable_metadata", errors)

    def test_command_snapshot_rejects_private_projected_authority_text(self) -> None:
        for field in ("summary", "observed_branch"):
            with self.subTest(field=field):
                meta = task(**{field: "token=do-not-project"})
                errors = handoffctl.resume_card_snapshot_errors(
                    [(Path("AR-0001.md"), meta, "")], handoffctl.DEFAULT_EVIDENCE_POLICY
                )
                self.assertTrue(any("possible credential" in error for error in errors))

    def test_command_snapshot_rejects_oversized_projected_text(self) -> None:
        for field, maximum in (
            ("title", resume_card.MAX_TITLE_BYTES),
            ("summary", resume_card.MAX_SUMMARY_BYTES),
            ("next_action", resume_card.MAX_NEXT_ACTION_BYTES),
        ):
            with self.subTest(field=field):
                meta = task(**{field: "x" * (maximum + 1)})
                errors = handoffctl.resume_card_snapshot_errors(
                    [(Path("AR-0001.md"), meta, "")], handoffctl.DEFAULT_EVIDENCE_POLICY
                )
                self.assertIn(f"AR-0001: {field} exceeds {maximum} bytes", errors)

    def test_command_snapshot_rejects_snapshot_without_an_aware_observation_time(self) -> None:
        meta = task(updated_at="2026-10-13T00:00:00")
        errors = handoffctl.resume_card_snapshot_errors(
            [(Path("AR-0001.md"), meta, "")], handoffctl.DEFAULT_EVIDENCE_POLICY
        )
        self.assertIn("AR-0001: updated_at is not timezone-aware", errors)

    def test_text_is_a_complete_equivalent_card_projection(self) -> None:
        card = resume_card.build(Path("/state"), Path("/projects"), task(), [task()])
        lines = resume_card.render_text(card).splitlines()
        self.assertEqual("RESUME_CARD=AR-0001", lines[0])
        projected = {
            name.lower(): json.loads(value)
            for name, value in (line.split("=", 1) for line in lines[1:])
        }
        self.assertEqual(card, projected)

    def test_cli_text_and_json_share_one_read_only_card(self) -> None:
        meta = task()
        card = resume_card.build(Path("/state"), Path("/projects"), meta, [meta])
        stream = io.StringIO()
        with (
            patch.object(handoffctl, "all_tasks", return_value=[(Path("task"), meta, "")]),
            patch.object(handoffctl, "config", return_value={"projects_root": "/projects"}),
            patch.object(
                handoffctl,
                "evidence_policy",
                return_value=handoffctl.DEFAULT_EVIDENCE_POLICY,
            ),
            patch.object(handoffctl, "resume_card_snapshot_errors", return_value=[]),
            patch.object(handoffctl, "read_only_snapshot", side_effect=lambda callback: callback()),
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

    def test_read_only_snapshot_creates_no_lock_or_trace_artifact(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = Path(directory) / ".runtime"
            trace = Path(directory) / "trace.jsonl"
            with (
                patch.object(
                    handoffctl,
                    "coordinator_lock_path",
                    return_value=runtime / "state.lock",
                ),
                patch.dict(os.environ, {"HANDOFFCTL_LOCK_TRACE": str(trace)}),
            ):
                self.assertEqual("card", handoffctl.read_only_snapshot(lambda: "card"))
            self.assertFalse(runtime.exists())
            self.assertFalse(trace.exists())

    def test_read_only_snapshot_never_traces_when_a_lock_already_exists(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = Path(directory) / ".runtime"
            runtime.mkdir()
            lock = runtime / "state.lock"
            lock.write_text("existing")
            trace = Path(directory) / "trace.jsonl"
            with (
                patch.object(handoffctl, "coordinator_lock_path", return_value=lock),
                patch.dict(os.environ, {"HANDOFFCTL_LOCK_TRACE": str(trace)}),
            ):
                self.assertEqual("card", handoffctl.read_only_snapshot(lambda: "card"))
            self.assertEqual("existing", lock.read_text())
            self.assertFalse(trace.exists())

        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="cmd", required=True)
        resume_card.add_command_parser(commands)
        self.assertEqual("AR-0001", parser.parse_args(["resume-card", "AR-0001", "--json"]).task)
