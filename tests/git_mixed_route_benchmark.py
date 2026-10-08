# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Disposable, real-CLI mixed-route Git contention benchmark.

Run ``python tests/git_mixed_route_benchmark.py``. Both pinned pre-repair and
current candidate receive the same per-worker route schedule and fixture.
Only bounded aggregates leave the disposable fixture; raw lock traces do not.
"""

import json
import math
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, cast

from git_contention_benchmark import COUNTS, fixture

ROUTES = ("snapshot", "doctor", "claim", "heartbeat", "update", "run", "release", "reconcile")
SCHEDULES = (
    ROUTES,
    ("claim", "snapshot", "heartbeat", "doctor", "update", "run", "reconcile", "release"),
    ("claim", "heartbeat", "snapshot", "update", "run", "doctor", "release", "reconcile"),
    ("claim", "heartbeat", "update", "snapshot", "run", "release", "doctor", "reconcile"),
)


def route_argv(route: str, index: int) -> list[str]:
    task = f"AR-{index:04d}"
    owner = f"worker-{index}"
    commands = {
        "snapshot": ["snapshot"],
        "doctor": ["doctor", "--live"],
        "claim": ["claim", task, "--owner", owner, "--lease-minutes", "120"],
        "heartbeat": ["heartbeat", task, "--owner", owner, "--lease-minutes", "120"],
        "update": [
            "update",
            task,
            "--owner",
            owner,
            "--expected-revision",
            "3",
            "--note",
            "Mixed-route fixture update.",
        ],
        "run": ["run", "--owner", owner, task, "true"],
        "release": [
            "release",
            task,
            "--owner",
            owner,
            "--status",
            "open",
            "--note",
            "Fixture complete.",
        ],
        "reconcile": ["reconcile", "--commit"],
    }
    return commands[route]


def worker(state: Path, index: int) -> int:
    """Execute one independent valid task journey and print bounded outcomes."""
    outcomes: list[dict[str, object]] = []
    claimed = False
    active = False
    for route in SCHEDULES[(index - 1) % len(SCHEDULES)]:
        if route in {"heartbeat", "update", "run", "release"} and not active:
            outcomes.append({"route": route, "result": "skipped"})
            continue
        started = time.monotonic()
        result = subprocess.run(  # noqa: S603
            [sys.executable, "tools/handoffctl.py", *route_argv(route, index)],
            cwd=state,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if route == "doctor" else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode == 0:
            classification = "ok"
        elif b"LOCK_TIMEOUT" in result.stderr:
            classification = "lock_timeout"
        elif route == "doctor" and b"stale" in result.stdout:
            classification = "diagnostic_stale"
        else:
            classification = "other_error"
        outcomes.append(
            {
                "route": route,
                "result": classification,
                "duration_ms": round((time.monotonic() - started) * 1000),
            }
        )
        if route == "claim" and classification == "ok":
            claimed = active = True
        elif route == "release" and classification == "ok":
            active = False
        elif route in {"heartbeat", "update", "run"} and classification != "ok":
            # A failed transition may have committed before a later error. Do
            # not infer a revision or issue dependent mutations after it.
            active = False
    print(json.dumps({"worker": index, "claimed": claimed, "outcomes": outcomes}, sort_keys=True))
    return 0


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))], 3)


def lock_events(item: dict[str, Any], wait: float, hold: float) -> list[tuple[int, str, int]]:
    """Emit half-open wait/hold intervals without zero-length wait artifacts."""
    end = int(item["ended_ns"])
    hold_ns = round(hold * 1_000_000)
    wait_ns = round(wait * 1_000_000)
    events: list[tuple[int, str, int]] = []
    if wait_ns > 0:
        events.extend(((end - hold_ns - wait_ns, "wait", 1), (end - hold_ns, "wait", -1)))
    if item["hold_ms"] is not None:
        events.extend(((end - hold_ns, "hold", 1), (end, "hold", -1)))
    return events


def lock_aggregates(traces: list[Path]) -> dict[str, object]:
    events: list[tuple[int, str, int]] = []
    by_phase: dict[str, dict[str, list[float]]] = {}
    for trace in traces:
        if not trace.exists():
            continue
        for line in trace.read_text().splitlines():
            item: dict[str, Any] = json.loads(line)
            phase = str(item["phase"])
            group = by_phase.setdefault(phase, {"wait": [], "hold": []})
            wait = float(item["wait_ms"])
            hold = float(item["hold_ms"] or 0)
            group["wait"].append(wait)
            if item["hold_ms"] is not None:
                group["hold"].append(hold)
            events.extend(lock_events(item, wait, hold))
    waiting = holding = peak = 0
    queue_samples: list[float] = []
    # A waiter stops waiting at the instant it acquires the lock. Process
    # interval ends before starts at equal timestamps to avoid counting the
    # new holder as its own queued waiter.
    for _, kind, delta in sorted(events, key=lambda event: (event[0], event[2] > 0)):
        if kind == "wait":
            waiting += delta
        else:
            holding += delta
        if holding:
            peak = max(peak, waiting)
            queue_samples.append(float(waiting))
    return {
        "max_wait_queue_depth": peak if by_phase else None,
        "wait_queue_depth_p50": percentile(queue_samples, 0.5),
        "wait_queue_depth_p95": percentile(queue_samples, 0.95),
        "phase_ms": {
            phase: {
                "events": len(values["wait"]),
                "wait_p50": percentile(values["wait"], 0.5),
                "wait_p95": percentile(values["wait"], 0.95),
                "wait_max": percentile(values["wait"], 1),
                "hold_p50": percentile(values["hold"], 0.5),
                "hold_p95": percentile(values["hold"], 0.95),
                "hold_max": percentile(values["hold"], 1),
            }
            for phase, values in sorted(by_phase.items())
        },
    }


def task_meta(state: Path, index: int) -> dict[str, Any]:
    """Audit a disposable authority file only after every CLI worker exits."""
    raw = (state / "tasks" / f"AR-{index:04d}.md").read_text()
    return cast(dict[str, Any], json.loads(raw.split("\n---\n", 1)[0][4:]))


def audit_state(state: Path, env: dict[str, str], count: int) -> dict[str, object]:
    """Bounded post-batch authority, projection, session and command audit."""
    doctor = subprocess.run(
        [sys.executable, "tools/handoffctl.py", "doctor", "--live"],
        cwd=state,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    result_path = state / ".runtime/command-results.jsonl"
    commands = (
        [json.loads(line) for line in result_path.read_text().splitlines()]
        if result_path.exists()
        else []
    )
    safe = complete = 0
    for index in range(1, count + 1):
        task_id = f"AR-{index:04d}"
        meta = task_meta(state, index)
        status = meta["status"]
        owner = meta["owner"]
        expiry = meta["claim_expires"]
        revision = int(meta["task_revision"])
        if (
            status in {"open", "in_progress"}
            and 1 <= revision <= 6
            and (
                (status == "open" and not owner and not expiry)
                or (status == "in_progress" and owner == f"worker-{index}" and expiry)
            )
        ):
            safe += 1
        session_path = state / "sessions" / f"{task_id}.jsonl"
        sessions = session_path.read_text().splitlines() if session_path.exists() else []
        recorded = sum(record["task"] == task_id for record in commands)
        if (
            status == "open"
            and not owner
            and not expiry
            and revision == 6
            and len(sessions) == 2
            and recorded == 1
        ):
            complete += 1
    return {
        "post_doctor_ok": doctor.returncode == 0,
        "safe_tasks": safe,
        "complete_tasks": complete,
        "command_results": len(commands),
    }


def batch(state: Path, env: dict[str, str], count: int) -> dict[str, object]:
    running: list[tuple[subprocess.Popen[bytes], Path]] = []
    started = time.monotonic()
    for index in range(1, count + 1):
        trace = state.parent / f"mixed-trace-{index}.jsonl"
        worker_env = dict(env)
        worker_env["HANDOFFCTL_LOCK_TRACE"] = str(trace)
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, __file__, "--worker", str(state), str(index)],
            env=worker_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        running.append((proc, trace))
    outputs: list[dict[str, Any]] = []
    process_errors = 0
    for proc, _ in running:
        try:
            stdout, _stderr = proc.communicate(timeout=max(1, 120 - (time.monotonic() - started)))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            process_errors += 1
            continue
        if proc.returncode:
            process_errors += 1
            continue
        outputs.append(json.loads(stdout))
    elapsed = time.monotonic() - started
    route_results: dict[str, dict[str, int]] = {}
    durations: dict[str, list[float]] = {}
    for output in outputs:
        for entry in output["outcomes"]:
            route = str(entry["route"])
            result = str(entry["result"])
            bucket = route_results.setdefault(route, {})
            bucket[result] = bucket.get(result, 0) + 1
            if "duration_ms" in entry:
                durations.setdefault(route, []).append(float(entry["duration_ms"]))
    successful = sum(values.get("ok", 0) for values in route_results.values())
    return {
        "workers": count,
        "wall_s": round(elapsed, 3),
        "successful_routes": successful,
        "throughput_routes_per_s": round(successful / elapsed, 3),
        "process_errors": process_errors,
        "route_results": route_results,
        "route_duration_ms": {
            route: {
                "p50": percentile(values, 0.5),
                "p95": percentile(values, 0.95),
                "max": percentile(values, 1),
            }
            for route, values in sorted(durations.items())
        },
        "locks": lock_aggregates([trace for _, trace in running]),
        "state_audit": audit_state(state, env, count),
    }


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        raise SystemExit(worker(Path(sys.argv[2]), int(sys.argv[3])))
    counts: tuple[int, ...] = COUNTS
    if len(sys.argv) == 3 and sys.argv[1] == "--counts":
        counts = tuple(int(value) for value in sys.argv[2].split(","))
        if not counts or any(value not in COUNTS for value in counts):
            raise SystemExit("counts must be a nonempty subset of 1,2,4,8,16")
    elif len(sys.argv) != 1:
        raise SystemExit("usage: python tests/git_mixed_route_benchmark.py [--counts 1,2,4,8,16]")
    with tempfile.TemporaryDirectory(prefix="awc-git-mixed-") as temp:
        base = Path(temp)
        for count in counts:
            for label, baseline in (("baseline", True), ("candidate", False)):
                state, env = fixture(base, f"{label}-{count}", baseline, initially_open=True)
                result = batch(state, env, count)
                print(json.dumps({"case": label, "result": result}, sort_keys=True), flush=True)
                audit = result["state_audit"]
                assert isinstance(audit, dict)
                if audit["safe_tasks"] != count:
                    raise RuntimeError("fixture authority safety audit failed")
                routes = cast(dict[str, dict[str, int]], result["route_results"])
                if not baseline and (
                    audit["complete_tasks"] != count
                    or not audit["post_doctor_ok"]
                    or result["process_errors"] != 0
                    or any(routes.get(route, {}).get("ok", 0) != count for route in ROUTES)
                ):
                    raise RuntimeError("candidate durable outcome audit failed")


if __name__ == "__main__":
    main()
