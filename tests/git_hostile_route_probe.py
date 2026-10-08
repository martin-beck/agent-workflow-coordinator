# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Disposable hostile Git handoffctl CLI probe for AR-0119.

Run ``python tests/git_hostile_route_probe.py``. Fixture setup is isolated;
every competing or recovering worker uses the actual handoffctl CLI. The
auditor reads authority files only after the CLI processes have exited.
"""

import datetime as dt
import json
import os
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from git_contention_benchmark import fixture
from git_mixed_route_benchmark import task_meta


def cli(
    state: Path, env: dict[str, str], *args: str, timeout: float = 30
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "tools/handoffctl.py", *args],
        cwd=state,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )


def require_success(result: subprocess.CompletedProcess[bytes], label: str) -> None:
    if result.returncode:
        raise RuntimeError(f"{label} failed")


def competing_claims(state: Path, env: dict[str, str]) -> None:
    contenders = []
    for owner in ("racer-a", "racer-b"):
        process = subprocess.Popen(  # noqa: S603
            [sys.executable, "tools/handoffctl.py", "claim", "AR-0001", "--owner", owner],
            cwd=state,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        contenders.append((owner, process))
    outcomes = []
    for owner, process in contenders:
        process.communicate(timeout=30)
        outcomes.append((owner, process.returncode))
    winners = [owner for owner, code in outcomes if code == 0]
    if len(winners) != 1 or len(outcomes) - len(winners) != 1:
        raise RuntimeError("competing claims did not have exactly one winner")
    winner = winners[0]
    loser = next(owner for owner, _ in outcomes if owner != winner)
    meta = task_meta(state, 1)
    if (meta["status"], meta["owner"], meta["task_revision"]) != ("in_progress", winner, 2):
        raise RuntimeError("competing claim authority is inconsistent")
    if cli(state, env, "heartbeat", "AR-0001", "--owner", loser).returncode == 0:
        raise RuntimeError("losing owner was allowed to heartbeat")
    if (
        cli(
            state,
            env,
            "release",
            "AR-0001",
            "--owner",
            loser,
            "--status",
            "open",
            "--note",
            "wrong owner",
        ).returncode
        == 0
    ):
        raise RuntimeError("losing owner was allowed to release")
    require_success(
        cli(
            state,
            env,
            "release",
            "AR-0001",
            "--owner",
            winner,
            "--status",
            "open",
            "--note",
            "race complete",
        ),
        "winning release",
    )


def interrupted_run(state: Path, env: dict[str, str]) -> None:
    owner = "interrupted-worker"
    require_success(cli(state, env, "claim", "AR-0002", "--owner", owner), "interrupt claim")
    process = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "tools/handoffctl.py",
            "run",
            "--owner",
            owner,
            "AR-0002",
            "sh",
            "-c",
            "printf READY; exec sleep 30",
        ],
        cwd=state,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 10)
        if not ready or process.stdout.read(5) != b"READY":
            raise RuntimeError("wrapped command did not start before interruption")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        process.communicate(timeout=5)
    meta = task_meta(state, 2)
    if (meta["status"], meta["owner"], meta["task_revision"]) != ("in_progress", owner, 2):
        raise RuntimeError("interrupted run invented a task transition")
    results = state / ".runtime/command-results.jsonl"
    if results.exists() and any(
        json.loads(line)["task"] == "AR-0002" for line in results.read_text().splitlines()
    ):
        raise RuntimeError("interrupted run recorded a false command outcome")
    require_success(
        cli(
            state,
            env,
            "release",
            "AR-0002",
            "--owner",
            owner,
            "--status",
            "open",
            "--note",
            "interrupted safely",
        ),
        "interrupt recovery release",
    )


def expired_lease(state: Path, env: dict[str, str]) -> None:
    owner = "expired-worker"
    require_success(
        cli(state, env, "claim", "AR-0003", "--owner", owner, "--lease-minutes", "1"),
        "expiring claim",
    )
    require_success(
        cli(
            state,
            env,
            "update",
            "AR-0003",
            "--owner",
            owner,
            "--expected-revision",
            "2",
            "--note",
            "durable session before expiry",
        ),
        "session update",
    )
    expiry = dt.datetime.fromisoformat(str(task_meta(state, 3)["claim_expires"]))
    time.sleep(max(0.0, (expiry - dt.datetime.now(dt.UTC)).total_seconds()) + 0.2)
    require_success(
        cli(
            state,
            env,
            "recover-expired",
            "AR-0003",
            "--expected-revision",
            "3",
            "--note",
            "expired owner recovered",
        ),
        "expired recovery",
    )
    if cli(state, env, "heartbeat", "AR-0003", "--owner", owner).returncode == 0:
        raise RuntimeError("expired owner regained its claim")
    require_success(
        cli(state, env, "claim", "AR-0003", "--owner", "new-worker"), "replacement claim"
    )
    require_success(
        cli(
            state,
            env,
            "release",
            "AR-0003",
            "--owner",
            "new-worker",
            "--status",
            "open",
            "--note",
            "replacement complete",
        ),
        "replacement release",
    )


def lock_timeout(state: Path, env: dict[str, str]) -> None:
    owner = "slow-writer"
    require_success(cli(state, env, "claim", "AR-0004", "--owner", owner), "slow writer claim")
    hooks = state.parent / "hooks"
    hooks.mkdir()
    marker = state.parent / "hook-started"
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\nprintf ready > {shlex.quote(str(marker))}\nsleep 12\n")
    hook.chmod(0o700)
    subprocess.run(  # noqa: S603
        ["git", "-C", str(state), "config", "core.hooksPath", str(hooks)],  # noqa: S607
        check=True,
    )
    process = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "tools/handoffctl.py",
            "update",
            "AR-0004",
            "--owner",
            owner,
            "--expected-revision",
            "2",
            "--note",
            "slow commit fixture",
        ],
        cwd=state,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.01)
        if not marker.exists():
            raise RuntimeError("slow commit hook was not reached")
        contender = cli(state, env, "claim", "AR-0005", "--owner", "waiting-worker", timeout=20)
        if contender.returncode == 0 or b"LOCK_TIMEOUT" not in contender.stderr:
            raise RuntimeError("contending CLI did not report bounded lock timeout")
        process.communicate(timeout=20)
        if process.returncode:
            raise RuntimeError("slow writer did not commit")
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
        subprocess.run(  # noqa: S603
            ["git", "-C", str(state), "config", "--unset", "core.hooksPath"],  # noqa: S607
            check=False,
        )
    if task_meta(state, 5)["task_revision"] != 1:
        raise RuntimeError("timed-out contender changed task authority")
    require_success(
        cli(state, env, "claim", "AR-0005", "--owner", "waiting-worker"), "post-timeout claim"
    )
    require_success(
        cli(
            state,
            env,
            "release",
            "AR-0005",
            "--owner",
            "waiting-worker",
            "--status",
            "open",
            "--note",
            "retry complete",
        ),
        "post-timeout release",
    )
    require_success(
        cli(
            state,
            env,
            "release",
            "AR-0004",
            "--owner",
            owner,
            "--status",
            "open",
            "--note",
            "writer complete",
        ),
        "slow writer release",
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="awc-git-hostile-") as temp:
        state, env = fixture(Path(temp), "candidate", False, initially_open=True)
        env["FIXTURE_GH_DELAY"] = "0"
        competing_claims(state, env)
        interrupted_run(state, env)
        expired_lease(state, env)
        lock_timeout(state, env)
        require_success(cli(state, env, "reconcile", "--commit"), "final reconcile")
        require_success(cli(state, env, "doctor", "--live"), "final doctor")
        if any(task_meta(state, index)["status"] != "open" for index in range(1, 6)):
            raise RuntimeError("hostile probe left an unresolved task")
        print(
            json.dumps(
                {
                    "competing_claims": "pass",
                    "interrupted_run": "pass",
                    "expired_lease": "pass",
                    "lock_timeout": "pass",
                    "final_doctor": "pass",
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
