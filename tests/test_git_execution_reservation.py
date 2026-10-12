# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Real independent-clone contention coverage for Git execution reservations."""

from __future__ import annotations

import argparse
import importlib.util
import json
import multiprocessing
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fixture_ids import project_uuid

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"


def git(*arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a real Git command for an isolated local remote fixture."""
    return subprocess.run(  # noqa: S603
        ["/usr/bin/git", *arguments],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )


def load_core(root: Path) -> Any:
    """Load an isolated coordinator module configured for one clone."""
    sys.path.insert(0, str(TOOLS))
    spec = importlib.util.spec_from_file_location(
        f"handoffctl_git_execution_reservation_{root.name}", TOOLS / "handoffctl.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load handoffctl")
    core: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
    core.ROOT = root
    core.TASKS = root / "tasks"
    core.RUNTIME = root / ".runtime"
    core.LOCK = core.RUNTIME / "state.lock"
    core.CONFIG = core.RUNTIME / "config.json"
    core.REPLICA_BLOCKED = core.RUNTIME / "replica-blocked.json"
    core.PROJECT_CONFIG = root / ".handoffctl.json"
    core.BINDING = root / "coordinator.binding.json"
    core.BACKEND_CONFIG = root / "coordinator.backend.json"
    core.DATABASE = core.RUNTIME / "coordinator.sqlite3"
    return core


def race_execution_start(
    clone: str,
    signing_key: str,
    synchronization: Any,
    outcomes: Any,
    execution_id: str,
) -> None:
    """Race a real Git-backed execution-start through a separate clone."""
    root = Path(clone)
    core = load_core(root)
    core.RUNTIME.mkdir(mode=0o700, exist_ok=True)
    core.CONFIG.write_text(json.dumps({"push_enabled": True}), encoding="utf-8")
    for name, value in (
        ("user.name", "Reservation Fixture"),
        ("user.email", "reservation@example.invalid"),
        ("gpg.format", "ssh"),
        ("user.signingkey", signing_key),
    ):
        git("config", name, value, cwd=root)

    # This makes both independently cloned replicas observe the same remote
    # baseline before either creates its signed reservation commit. The race
    # therefore exercises remote non-fast-forward admission, not scheduling.
    original_sync = core.sync_replica_before_write

    def synchronized_sync() -> None:
        original_sync()
        synchronization.wait(timeout=20)

    core.sync_replica_before_write = synchronized_sync
    args = argparse.Namespace(
        task="AR-0001",
        owner="worker-a",
        expected_revision=1,
        execution_id=execution_id,
        execution_expires_at="2099-01-01T00:00:00+00:00",
        command_class="test",
        scope_digest="sha256:" + "b" * 64,
        argv_digest="c" * 64,
    )
    try:
        core.mutate(args, "execution-start")
    except Exception as error:  # The losing push must not be treated as admission.
        outcomes.put(("rejected", execution_id, str(error)))
    else:
        # `_cmd_run_fenced` starts a subprocess only after this return.
        outcomes.put(("admitted", execution_id, ""))


class GitExecutionReservationTest(unittest.TestCase):
    """Prove one remote Git authority admits at most one execution reservation."""

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.seed = self.root / "seed"
        self.remote = self.root / "remote.git"
        self.clone_a = self.root / "clone-a"
        self.clone_b = self.root / "clone-b"
        self.key = self.root / "reservation-signing-key"
        self._create_seed()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _create_seed(self) -> None:
        """Create a valid bound-envelope task and a locally hosted Git remote."""
        git("init", "-q", "--initial-branch=main", str(self.seed))
        core = load_core(self.seed)
        core.TASKS.mkdir()
        (self.seed / "plans").mkdir()
        core.PROJECT_CONFIG.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project_id": project_uuid("1"),
                    "project_name": "reservation-fixture",
                    "project_title": "Reservation Fixture",
                    "status_view": True,
                    "commit_signoff": False,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        core.BINDING.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project_id": project_uuid("1"),
                    "state_repository": "owner/state",
                    "product_repository": "owner/product",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        envelope = {
            "schema_version": 1,
            "envelope_id": "AR-0001-e1",
            "envelope_revision": 1,
            "task": "AR-0001",
            "task_revision": 1,
            "spec_ref": "spec.json",
            "spec_revision": 1,
            "outputs": [{"id": "output", "description": "bounded output"}],
            "non_goals": ["no expansion"],
            "path_scope": {"read": ["src"], "write": ["src"]},
            "command_classes": {"test": {"allowed": True}},
            "evidence": [{"id": "tests", "class": "contract-test", "required": True}],
            "stop_conditions": [{"id": "stop", "kind": "deny"}],
        }
        meta = {
            "schema_version": 1,
            "id": "AR-0001",
            "title": "Reservation contention",
            "status": "in_progress",
            "priority": "P1",
            "summary": "Ready.",
            "next_action": "Race reservations.",
            "task_revision": 1,
            "updated_at": "2026-10-12T00:00:00+00:00",
            "owner": "worker-a",
            "claim_expires": "2099-01-01T00:00:00+00:00",
            "worktree_key": "",
            "branch": "",
            "checkpoint_commit": "",
            "plan": "",
            "depends_on": [],
            "execution_envelope_ref": "envelopes/AR-0001-e1.json",
            "execution_envelope_revision": 1,
            "execution_envelope_task_revision": 1,
            "execution_envelope_digest": core.execution_envelope.canonical_digest(envelope),
            "execution_envelope": envelope,
        }
        core.write_task(core.TASKS / "AR-0001-test.md", meta, "# Test\n")
        tasks = core.all_tasks()
        core.atomic(self.seed / "CURRENT.md", core.render_current(tasks))
        core.atomic(self.seed / "STATUS.md", core.render_status_view(tasks))
        git("add", ".", cwd=self.seed)
        git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "seed",
            cwd=self.seed,
        )
        git("init", "--bare", "-q", "--initial-branch=main", str(self.remote))
        git("remote", "add", "origin", str(self.remote), cwd=self.seed)
        git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=self.seed)
        git("clone", "-q", str(self.remote), str(self.clone_a))
        git("clone", "-q", str(self.remote), str(self.clone_b))
        subprocess.run(  # noqa: S603
            ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key)],
            check=True,
        )

    def test_independent_clones_admit_only_remote_reservation_winner(self) -> None:
        """A non-fast-forward loser cannot reach the post-reservation run boundary."""
        context = multiprocessing.get_context("spawn")
        synchronization = context.Barrier(2)
        outcomes = context.Queue()
        workers = [
            context.Process(
                target=race_execution_start,
                args=(
                    str(clone),
                    str(self.key),
                    synchronization,
                    outcomes,
                    execution_id,
                ),
            )
            for clone, execution_id in (
                (self.clone_a, "a" * 32),
                (self.clone_b, "d" * 32),
            )
        ]
        for worker in workers:
            worker.start()
        results = [outcomes.get(timeout=30) for _ in workers]
        for worker in workers:
            worker.join(timeout=30)
            self.assertEqual(0, worker.exitcode)

        admitted = [result for result in results if result[0] == "admitted"]
        rejected = [result for result in results if result[0] == "rejected"]
        self.assertEqual(1, len(admitted), results)
        self.assertEqual(1, len(rejected), results)
        self.assertIn("command failed", rejected[0][2])

        observer = self.root / "observer"
        git("clone", "-q", str(self.remote), str(observer))
        core = load_core(observer)
        _path, meta, _body = core.locate("AR-0001")
        reservation = meta["execution_reservation"]
        self.assertEqual(admitted[0][1], reservation["execution_id"])
        self.assertEqual("worker-a", reservation["owner"])
        self.assertEqual(2, meta["task_revision"])


if __name__ == "__main__":
    unittest.main()
