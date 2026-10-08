# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Disposable real-CLI Git-backend contention benchmark (not a unit test)."""

import datetime as dt
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

SOURCE = Path(__file__).resolve().parents[1]
BASELINE_COMMIT = "113dc61029f0e0c57bc7832e1e41430eafa17e73"
sys.path.insert(0, str(SOURCE))
from tools.vendor import SOURCE_FILES  # noqa: E402

COUNTS = (1, 2, 4, 8, 16)
WORKERS = max(COUNTS)


def command(*argv: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(argv, cwd=cwd, env=env, check=True, stdout=subprocess.DEVNULL)  # noqa: S603


def fixture(
    base: Path, label: str, baseline: bool, *, initially_open: bool = False
) -> tuple[Path, dict[str, str]]:
    state = base / label / "state"
    product = base / label / "product"
    state.mkdir(parents=True)
    product.mkdir()
    for source_name, destination_name in SOURCE_FILES:
        if not source_name.startswith("tools/"):
            continue
        target = state / destination_name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE / source_name, target)
    shutil.copytree(SOURCE / "schema", state / "schema")
    if baseline:
        old = subprocess.run(  # noqa: S603
            ["git", "show", f"{BASELINE_COMMIT}:tools/handoffctl.py"],  # noqa: S607
            cwd=SOURCE,
            check=True,
            capture_output=True,
        ).stdout
        (state / "tools/handoffctl.py").write_bytes(old)
    (state / "tasks").mkdir()
    (state / "plans").mkdir()
    (state / ".runtime").mkdir()
    (state / ".gitignore").write_text(".runtime/\n")
    project_id = str(uuid.uuid4())
    (state / ".handoffctl.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": project_id,
                "project_name": "contention-fixture",
                "project_title": "Contention Fixture",
                "status_view": True,
                "commit_signoff": False,
            }
        )
    )
    (state / "coordinator.binding.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": project_id,
                "state_repository": "fixture/state",
                "product_repository": "fixture/product",
            }
        )
    )
    (state / "coordinator.backend.json").write_text(
        json.dumps({"schema_version": 1, "project_id": project_id, "backend": "git"})
    )
    (state / ".runtime/config.json").write_text(
        json.dumps(
            {
                "projects_root": str(base / label),
                "product_worktree": "product",
                "github_repository": "fixture/product",
                "push_enabled": False,
            }
        )
    )
    expiry = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=2)).isoformat()
    for index in range(1, WORKERS + 1):
        task_id = f"AR-{index:04d}"
        meta = {
            "schema_version": 1,
            "id": task_id,
            "title": "Contention fixture",
            "status": "open" if initially_open else "in_progress",
            "priority": "P1",
            "summary": "Independent worker fixture.",
            "next_action": "Run command.",
            "task_revision": 1,
            "updated_at": dt.datetime.now(dt.UTC).isoformat(),
            "owner": "" if initially_open else f"worker-{index}",
            "claim_expires": "" if initially_open else expiry,
            "worktree_key": "",
            "branch": "",
            "checkpoint_commit": "",
            "plan": "",
            "depends_on": [],
        }
        (state / "tasks" / f"{task_id}.md").write_text(
            "---\n" + json.dumps(meta, indent=2) + "\n---\n\nFixture.\n"
        )
    command("git", "init", "-q", "-b", "main", str(product))
    (product / ".gitignore").write_text("owner/\n")
    (product / "README.md").write_text("# Fixture\n")
    command("git", "-C", str(product), "config", "user.name", "Fixture")
    command("git", "-C", str(product), "config", "user.email", "fixture@example.invalid")
    command("git", "-C", str(product), "add", ".")
    command("git", "-C", str(product), "commit", "-qm", "fixture")
    (product / "fixture").mkdir()
    command("git", "clone", "-q", "--bare", str(product), str(product / "fixture/product"))
    command("git", "-C", str(product), "remote", "add", "origin", "fixture/product")
    command("git", "-C", str(product), "fetch", "-q", "origin", "main")
    command("git", "init", "-q", "-b", "main", str(state))
    command("git", "-C", str(state), "remote", "add", "origin", "fixture/state")
    command("git", "-C", str(state), "config", "user.name", "Fixture")
    command("git", "-C", str(state), "config", "user.email", "fixture@example.invalid")
    key = base / label / "signing-key"
    command("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key))
    command("git", "-C", str(state), "config", "gpg.format", "ssh")
    command("git", "-C", str(state), "config", "user.signingkey", str(key))
    command("git", "-C", str(state), "add", ".")
    command("git", "-C", str(state), "commit", "-q", "-S", "-m", "fixture")
    bin_dir = base / label / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import os, time\n"
        "time.sleep(float(os.environ.get('FIXTURE_GH_DELAY', '0.6')))\n"
        "print('[]')\n"
    )
    gh.chmod(0o700)
    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    env["FIXTURE_GH_DELAY"] = "0.6"
    command(sys.executable, "tools/handoffctl.py", "reconcile", "--commit", cwd=state, env=env)
    return state, env


def benchmark(state: Path, env: dict[str, str]) -> list[dict[str, object]]:  # noqa: C901
    results: list[dict[str, object]] = []
    for count in COUNTS:
        running = []
        started = time.monotonic()
        for index in range(1, count + 1):
            trace = state.parent / f"trace-{count}-{index}.jsonl"
            worker_env = dict(env)
            worker_env["HANDOFFCTL_LOCK_TRACE"] = str(trace)
            argv = [
                sys.executable,
                "tools/handoffctl.py",
                "run",
                "--owner",
                f"worker-{index}",
                f"AR-{index:04d}",
                "true",
            ]
            process = subprocess.Popen(  # noqa: S603
                argv, cwd=state, env=worker_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
            running.append((process, trace, time.monotonic()))
        latencies = []
        errors = []
        metrics: list[dict[str, Any]] = []
        finished: dict[int, tuple[float, bytes]] = {}
        while len(finished) < len(running):
            if time.monotonic() - started > 90:
                raise TimeoutError("contention fixture exceeded 90 seconds")
            for process, _trace, _launch in running:
                if process.pid not in finished and process.poll() is not None:
                    _, stderr = process.communicate()
                    finished[process.pid] = (time.monotonic(), stderr)
            time.sleep(0.005)
        for process, trace, launch in running:
            completed, stderr = finished[process.pid]
            latencies.append(round(completed - launch, 3))
            if process.returncode:
                errors.append("LOCK_TIMEOUT" if b"LOCK_TIMEOUT" in stderr else "OTHER")
            if trace.exists():
                metrics.extend(json.loads(line) for line in trace.read_text().splitlines())
        waits = [float(item["wait_ms"]) for item in metrics]
        holds = [float(item["hold_ms"]) for item in metrics if item["hold_ms"] is not None]
        events: list[tuple[int, str, int]] = []
        phases: dict[str, list[dict[str, Any]]] = {}
        for item in metrics:
            phases.setdefault(str(item["phase"]), []).append(item)
            end = int(item["ended_ns"])
            wait_ns = round(float(item["wait_ms"]) * 1_000_000)
            hold_ns = round(float(item["hold_ms"] or 0) * 1_000_000)
            events.extend(((end - wait_ns - hold_ns, "wait", 1), (end - hold_ns, "wait", -1)))
            if item["hold_ms"] is not None:
                events.extend(((end - hold_ns, "hold", 1), (end, "hold", -1)))
        waiting = 0
        holding = 0
        max_queue_depth = 0
        for _, kind, delta in sorted(events):
            if kind == "wait":
                waiting += delta
            else:
                holding += delta
            if holding:
                max_queue_depth = max(max_queue_depth, waiting)
        results.append(
            {
                "workers": count,
                "wall_s": round(time.monotonic() - started, 3),
                "throughput_per_s": round((count - len(errors)) / (time.monotonic() - started), 3),
                "latency_median_s": round(statistics.median(latencies), 3),
                "latency_max_s": max(latencies),
                "lock_wait_max_ms": max(waits, default=None),
                "lock_hold_max_ms": max(holds, default=None),
                "lock_timeouts": errors.count("LOCK_TIMEOUT"),
                "other_errors": errors.count("OTHER"),
                "lock_events": len(metrics),
                "max_queue_depth": max_queue_depth if metrics else None,
                "phase_max_wait_ms": {
                    name: max(float(item["wait_ms"]) for item in records)
                    for name, records in phases.items()
                },
                "phase_max_hold_ms": {
                    name: max(float(item["hold_ms"] or 0) for item in records)
                    for name, records in phases.items()
                },
            }
        )
    return results


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="awc-git-contention-") as temp:
        base = Path(temp)
        for label, baseline in (("baseline", True), ("candidate", False)):
            state, env = fixture(base, label, baseline)
            print(json.dumps({"case": label, "results": benchmark(state, env)}, sort_keys=True))


if __name__ == "__main__":
    main()
