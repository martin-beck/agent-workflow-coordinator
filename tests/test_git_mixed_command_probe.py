# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Safety checks for 16-lane Git-backed mixed-route classification."""

import datetime as dt
import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from git_mixed_command_probe import (
    TASK_IDS,
    acceptance_errors,
    acceptances,
    adversarial_commands,
    checked_batch,
    error_class,
    fast_hostile_commands,
    has_matching_dco_trailer,
    heartbeat_effect_errors,
    receipt_queue_errors,
    require_unchanged_sources,
    route_name,
    stabilize_disposable_claims,
    strict_acceptance_commit_errors,
)


class GitMixedCommandProbeTests(unittest.TestCase):
    def test_heartbeat_effect_checker_rejects_noop_wrong_lease_and_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            tasks = state / "tasks"
            tasks.mkdir()
            now = dt.datetime.now(dt.UTC).replace(microsecond=0)
            before: dict[str, dict[str, object]] = {}
            bodies: dict[str, str] = {}
            for index, task_id in enumerate(TASK_IDS):
                body = "\n\nFixture body.\n"
                bodies[task_id] = body
                old = {
                    "id": task_id,
                    "status": "in_progress",
                    "owner": f"bench-{index}",
                    "task_revision": 2,
                    "claim_expires": (now + dt.timedelta(hours=2)).isoformat(),
                    "updated_at": (now - dt.timedelta(minutes=1)).isoformat(),
                }
                before[task_id] = old
                new = dict(old)
                new["task_revision"] = 3
                new["claim_expires"] = (now + dt.timedelta(minutes=20)).isoformat()
                new["updated_at"] = now.isoformat()
                (tasks / f"{task_id}.md").write_text(
                    "---\n"
                    + json.dumps(new)
                    + "\n---"
                    + body
                    + f"\n- {new['updated_at']}: Heartbeat by bench-{index}.\n"
                )
            self.assertEqual(
                [],
                heartbeat_effect_errors(state, before, bodies, now, now + dt.timedelta(seconds=1)),
            )
            target = tasks / f"{TASK_IDS[0]}.md"
            original = target.read_text()
            target.write_text(original.replace('"task_revision": 3', '"task_revision": 2'))
            self.assertTrue(
                any(
                    "revision did not advance" in error
                    for error in heartbeat_effect_errors(state, before, bodies, now, now)
                )
            )
            target.write_text(
                original.replace(
                    (now + dt.timedelta(minutes=20)).isoformat(),
                    (now + dt.timedelta(minutes=2)).isoformat(),
                )
            )
            self.assertTrue(
                any(
                    "lease does not match" in error
                    for error in heartbeat_effect_errors(state, before, bodies, now, now)
                )
            )
            target.write_text(original.replace("Heartbeat by bench-0.", "No heartbeat."))
            self.assertTrue(
                any(
                    "history/body does not match" in error
                    for error in heartbeat_effect_errors(state, before, bodies, now, now)
                )
            )
            target.write_text(
                original.replace(
                    f'"updated_at": "{now.isoformat()}"',
                    f'"updated_at": "{(now - dt.timedelta(minutes=5)).isoformat()}"',
                )
            )
            self.assertTrue(
                any(
                    "lease duration differs" in error
                    for error in heartbeat_effect_errors(
                        state, before, bodies, now - dt.timedelta(minutes=5), now
                    )
                )
            )

    def test_receipt_queue_checker_rejects_extra_durable_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            private = state / "private"
            private.mkdir()
            project_id = "11111111-1111-4111-8111-111111111111"
            (state / "coordinator.binding.json").write_text(json.dumps({"project_id": project_id}))
            database = private / "fast-receipts.sqlite3"
            completed: dict[str, dict[str, object]] = {}
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE receipt_binding(singleton INT, project_id TEXT)")
                connection.execute("INSERT INTO receipt_binding VALUES (1, ?)", (project_id,))
                connection.execute(
                    "CREATE TABLE intents(receipt_id TEXT, project_id TEXT, "
                    "idempotency_key TEXT, operation TEXT, task_id TEXT, "
                    "expected_revision INT, payload_json TEXT, input_digest TEXT, "
                    "phase TEXT, result_revision INT, error_code TEXT, remote_oid TEXT, "
                    "commit_oid TEXT)"
                )
                for index, task_id in enumerate(TASK_IDS):
                    receipt_id = f"{index:032x}"
                    commit_oid = f"{index:040x}"
                    payload = {
                        "expected_revision": 2,
                        "lease_minutes": 20,
                        "operation": "heartbeat",
                        "owner": f"bench-{index}",
                        "project_id": project_id,
                        "task": task_id,
                    }
                    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
                    completed[receipt_id] = {
                        "project_id": project_id,
                        "operation": "heartbeat",
                        "task_id": task_id,
                        "expected_revision": 2,
                        "phase": "completed-local",
                        "commit_oid": commit_oid,
                        "result_revision": 3,
                        "error_code": None,
                        "remote_oid": None,
                    }
                    connection.execute(
                        "INSERT INTO intents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            receipt_id,
                            project_id,
                            f"bench-{index}:heartbeat:2",
                            "heartbeat",
                            task_id,
                            2,
                            canonical,
                            hashlib.sha256(canonical.encode()).hexdigest(),
                            "completed-local",
                            3,
                            None,
                            None,
                            commit_oid,
                        ),
                    )
            with mock.patch(
                "git_mixed_command_probe.coordinator_private_root", return_value=private
            ):
                self.assertEqual([], receipt_queue_errors(state, completed))
                with sqlite3.connect(database) as connection:
                    connection.execute(
                        "INSERT INTO intents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            "extra",
                            project_id,
                            "extra",
                            "heartbeat",
                            TASK_IDS[0],
                            2,
                            "{}",
                            "bad",
                            "queued-local",
                            None,
                            None,
                            None,
                            None,
                        ),
                    )
                self.assertTrue(
                    any(
                        "missing or extra durable intents" in error
                        for error in receipt_queue_errors(state, completed)
                    )
                )
                with sqlite3.connect(database) as connection:
                    connection.execute("DELETE FROM intents WHERE receipt_id='extra'")
                    rows = connection.execute(
                        "SELECT receipt_id, idempotency_key, task_id, payload_json, "
                        "input_digest FROM intents WHERE receipt_id IN (?, ?) ORDER BY receipt_id",
                        ("0" * 32, "0" * 31 + "1"),
                    ).fetchall()
                    for current, other in ((rows[0], rows[1]), (rows[1], rows[0])):
                        connection.execute(
                            "UPDATE intents SET idempotency_key=?, task_id=?, "
                            "payload_json=?, input_digest=? WHERE receipt_id=?",
                            (*other[1:], current[0]),
                        )
                self.assertTrue(
                    any(
                        "does not match public receipt" in error
                        for error in receipt_queue_errors(state, completed)
                    )
                )

    def test_dco_requires_actual_matching_final_trailer(self) -> None:
        identity = "Fixture <fixture@example.invalid>"
        self.assertTrue(
            has_matching_dco_trailer(
                f"subject\n\nSigned-off-by: {identity}\n", "Fixture", "fixture@example.invalid"
            )
        )
        self.assertFalse(
            has_matching_dco_trailer(
                f"subject\n\nMention Signed-off-by: {identity} in prose.\n",
                "Fixture",
                "fixture@example.invalid",
            )
        )
        self.assertFalse(
            has_matching_dco_trailer(
                f"subject\n\nSigned-off-by: {identity}\n", "Other", "other@example.invalid"
            )
        )

    def test_acceptance_commit_checker_rejects_unsigned_or_missing_dco(self) -> None:
        def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
            if argv[1] == "rev-list":
                return subprocess.CompletedProcess(argv, 0, "abc123\n")
            if argv[1] == "diff-tree":
                return subprocess.CompletedProcess(argv, 0, b"tasks/AR-9000.md\0")
            if argv[1] == "verify-commit":
                return subprocess.CompletedProcess(argv, 1, b"", b"invalid signature")
            if argv[1] == "show" and argv[3] == "--format=%P":
                return subprocess.CompletedProcess(argv, 0, "parent\n")
            if argv[1] == "show" and argv[3] == "--format=%an%x00%ae%x00%s%x00%B":
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    "Fixture\0fixture@example.invalid\0chore(state): accept AR-9000\0missing",
                )
            if argv[1] == "interpret-trailers":
                return subprocess.CompletedProcess(argv, 0, "")
            raise AssertionError(argv)

        with (
            mock.patch("git_mixed_command_probe.subprocess.run", side_effect=fake_run),
            mock.patch("git_mixed_command_probe.history_extends", return_value=True),
        ):
            count, errors = strict_acceptance_commit_errors(Path("disposable"), "starting")
        self.assertEqual(1, count)
        self.assertTrue(any("signature did not verify" in error for error in errors))
        self.assertTrue(any("DCO trailer missing" in error for error in errors))

    def test_acceptance_commit_checker_catches_intermediate_unrelated_path(self) -> None:
        def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
            if argv[1] == "rev-list":
                return subprocess.CompletedProcess(argv, 0, "newer\nolder\n")
            if argv[1] == "diff-tree":
                path = b"secret.txt\0" if argv[-1] == "older" else b"tasks/AR-9001.md\0"
                return subprocess.CompletedProcess(argv, 0, path)
            if argv[1] == "verify-commit":
                return subprocess.CompletedProcess(argv, 0, b"")
            if argv[1] == "show" and argv[3] == "--format=%P":
                return subprocess.CompletedProcess(argv, 0, "parent\n")
            if argv[1] == "show" and argv[3] == "--format=%an%x00%ae%x00%s%x00%B":
                task_id = "AR-9000" if argv[-1] == "older" else "AR-9001"
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    f"Fixture\0fixture@example.invalid\0chore(state): accept {task_id}\0subject\n",
                )
            if argv[1] == "interpret-trailers":
                return subprocess.CompletedProcess(
                    argv, 0, "Signed-off-by: Fixture <fixture@example.invalid>\n"
                )
            raise AssertionError(argv)

        with (
            mock.patch("git_mixed_command_probe.subprocess.run", side_effect=fake_run),
            mock.patch("git_mixed_command_probe.history_extends", return_value=True),
        ):
            _, errors = strict_acceptance_commit_errors(Path("disposable"), "starting")
        self.assertTrue(any("unrelated committed path changed" in error for error in errors))

    def test_acceptance_errors_reject_missing_or_wrong_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tasks = Path(directory) / "tasks"
            tasks.mkdir()
            before: dict[str, dict[str, object]] = {}
            for index, task_id in enumerate(TASK_IDS):
                meta = {
                    "owner": f"bench-{index}",
                    "task_revision": 3,
                    "spec_ref": f"specs/{task_id}.json",
                    "spec_revision": 1,
                    "spec_acceptance": {
                        "spec_ref": f"specs/{task_id}.json",
                        "spec_revision": 1,
                        "status": "pass",
                        "evidence_class": "mechanical",
                        "evidence_ref": f"quality/{task_id}",
                        "evidence_digest": "sha256:" + "a" * 64,
                    },
                }
                before[task_id] = {
                    key: value for key, value in meta.items() if key != "spec_acceptance"
                }
                before[task_id]["task_revision"] = 2
                (tasks / f"{task_id}.md").write_text("---\n" + json.dumps(meta) + "\n---\n")
            self.assertEqual([], acceptance_errors(Path(directory), before))
            bad = tasks / f"{TASK_IDS[0]}.md"
            meta = json.loads(bad.read_text().split("---", 2)[1])
            del meta["spec_acceptance"]
            bad.write_text("---\n" + json.dumps(meta) + "\n---\n")
            self.assertIn(
                "acceptance does not match request", acceptance_errors(Path(directory), before)[0]
            )
            meta["unrelated"] = "changed"
            bad.write_text("---\n" + json.dumps(meta) + "\n---\n")
            self.assertTrue(
                any(
                    "unrelated task fields changed" in error
                    for error in acceptance_errors(Path(directory), before)
                )
            )

    def test_acceptances_bind_each_owner_revision_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tasks = Path(directory) / "tasks"
            tasks.mkdir()
            for index, task_id in enumerate(TASK_IDS):
                (tasks / f"{task_id}.md").write_text(
                    "---\n" + json.dumps({"task_revision": index + 2}) + "\n---\n"
                )
            commands = acceptances(Path(directory))
            self.assertEqual(16, len(commands))
            self.assertEqual(16, len({tuple(command) for command in commands}))
            for index, command in enumerate(commands):
                self.assertEqual(["accept", TASK_IDS[index]], command[:2])
                self.assertEqual(f"bench-{index}", command[3])
                self.assertEqual(str(index + 2), command[5])
                self.assertEqual("mechanical", command[7])
                self.assertEqual(f"quality/{TASK_IDS[index]}", command[9])

    def test_stabilize_claims_only_changes_disposable_active_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tasks = Path(directory) / "tasks"
            tasks.mkdir()
            claimed = tasks / "AR-9000.md"
            open_task = tasks / "AR-9001.md"
            future_task = tasks / "AR-9002.md"
            expired = "2020-01-01T00:00:00+00:00"
            claimed.write_text(
                "---\n"
                + json.dumps({"status": "in_progress", "claim_expires": expired}, sort_keys=True)
                + "\n---\n\nKeep this body.\n"
            )
            open_task.write_text(
                "---\n"
                + json.dumps({"status": "open", "claim_expires": ""}, sort_keys=True)
                + "\n---\n\nKeep this open task.\n"
            )
            future = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=2)).isoformat()
            future_task.write_text(
                "---\n"
                + json.dumps({"status": "in_progress", "claim_expires": future}, sort_keys=True)
                + "\n---\n\nKeep this future task.\n"
            )
            before_open = open_task.read_bytes()
            before_future = future_task.read_bytes()
            self.assertEqual(1, stabilize_disposable_claims(Path(directory)))
            self.assertEqual(before_open, open_task.read_bytes())
            self.assertEqual(before_future, future_task.read_bytes())
            self.assertTrue(claimed.read_text().endswith("\n\nKeep this body.\n"))
            metadata = json.loads(claimed.read_text().split("---", 2)[1])
            self.assertGreater(
                dt.datetime.fromisoformat(metadata["claim_expires"]), dt.datetime.now(dt.UTC)
            )

    def test_stabilize_claims_rejects_malformed_source_without_rewriting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tasks = Path(directory) / "tasks"
            tasks.mkdir()
            expired = tasks / "AR-9000.md"
            malformed = tasks / "AR-9001.md"
            expired.write_text(
                "---\n"
                + json.dumps(
                    {"status": "in_progress", "claim_expires": "2020-01-01T00:00:00+00:00"}
                )
                + "\n---\n"
            )
            malformed.write_text(
                "---\n"
                + json.dumps({"status": "in_progress", "claim_expires": "invalid"})
                + "\n---\n"
            )
            before = expired.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "invalid source claim expiry"):
                stabilize_disposable_claims(Path(directory))
            self.assertEqual(before, expired.read_bytes())

    def test_route_names_keep_semantically_distinct_variants(self) -> None:
        self.assertEqual("doctor-live", route_name(["doctor", "--live"]))
        self.assertEqual("doctor", route_name(["doctor"]))
        self.assertEqual("render-check", route_name(["render-status", "--check"]))
        self.assertEqual("roles-assign", route_name(["roles", "assign"]))

    def test_expected_rejections_are_not_success(self) -> None:
        self.assertEqual("lock_timeout", error_class(b"LOCK_TIMEOUT"))
        self.assertEqual("wrong_owner", error_class(b"AR-9000 is owned by bench-0"))
        self.assertEqual("wrong_owner", error_class(b"task claim does not match owner"))
        self.assertEqual("stale_task_revision", error_class(b"stale revision: expected 2"))
        self.assertEqual("malformed_gate_artifact", error_class(b"--before must use REF=DIGEST"))
        self.assertEqual("stale_role_revision", error_class(b"stale role revision"))
        self.assertEqual(
            "unsupported_git_route", error_class(b"board requires the SQLite authority")
        )
        self.assertEqual("usage_error", error_class(b"usage: handoffctl"))

    def test_adversarial_batch_has_one_legitimate_worker(self) -> None:
        commands = adversarial_commands(4, Path("disposable-unauthorized-marker"))
        self.assertEqual(16, len(commands))
        self.assertEqual(
            ["update", "AR-9000", "--owner", "bench-0", "--expected-revision", "4"],
            commands[-1][:6],
        )
        self.assertEqual(15, sum(command != commands[-1] for command in commands))
        self.assertEqual(2, sum("write_text" in " ".join(command) for command in commands))

    def test_fast_hostile_batch_has_one_good_and_fifteen_competing_intents(self) -> None:
        commands = fast_hostile_commands()
        self.assertEqual(16, len(commands))
        self.assertEqual(16, len({command[-1] for command in commands}))
        self.assertEqual(
            1, sum(command[6] == "2" and command[4] == "bench-0" for command in commands)
        )
        self.assertEqual(8, sum(command[4] != "bench-0" for command in commands))
        self.assertEqual(7, sum(command[6] == "1" for command in commands))

    def test_source_drift_fails_probe(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "source inputs changed"):
            require_unchanged_sources(
                {"source_product_inputs_changed": True, "source_state_head_changed": False}
            )

    def test_mixed_batch_fails_on_lost_worker_or_bad_doctor(self) -> None:
        with (
            mock.patch(
                "git_mixed_command_probe.batch", return_value={"routes": {"claim": {"ok": 15}}}
            ),
            mock.patch(
                "git_mixed_command_probe.doctor", return_value={"doctor": 0, "doctor_live": 0}
            ),
            self.assertRaisesRegex(RuntimeError, "unexpected route outcome"),
        ):
            checked_batch(Path("disposable-state"), {}, "claims", [], 30, 16)
        with (
            mock.patch(
                "git_mixed_command_probe.batch", return_value={"routes": {"claim": {"ok": 16}}}
            ),
            mock.patch(
                "git_mixed_command_probe.doctor", return_value={"doctor": 0, "doctor_live": 1}
            ),
            self.assertRaisesRegex(RuntimeError, "failed integrity check"),
        ):
            checked_batch(Path("disposable-state"), {}, "claims", [], 30, 16)


if __name__ == "__main__":
    unittest.main()
