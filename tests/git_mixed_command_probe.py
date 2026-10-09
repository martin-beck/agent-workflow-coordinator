# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Exercise 16 simultaneous Git-backed commands on one disposable ASB state.

The source repositories are observed only. Every mutation is confined to a
temporary state clone, and the probe reports failures rather than retrying or
silently treating contention as success.
"""

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, cast

from git_command_latency_benchmark import (
    configure,
    git_head,
    prepare_runtime,
    product_input_digest,
    worktree_listing,
)
from git_scale_probe import prepare

TASK_IDS = tuple(f"AR-{number:04d}" for number in range(9000, 9016))


def task_meta(state: Path, task_id: str) -> dict[str, Any]:
    path = state / "tasks" / f"{task_id}.md"
    return cast(dict[str, Any], json.loads(path.read_text().split("---", 2)[1]))


def add_fixture_tasks(state: Path, env: dict[str, str]) -> None:
    template = json.loads((state / "tasks/AR-1686.md").read_text().split("---", 2)[1])
    spec = json.loads((state / "specs/AR-1686.json").read_text())
    for task_id in TASK_IDS:
        reference = f"specs/{task_id}.json"
        item = dict(template)
        item.update(
            {
                "id": task_id,
                "title": f"Disposable concurrency fixture {task_id}",
                "summary": "Exercise simultaneous Git-backed coordinator commands.",
                "next_action": "Run the disposable mixed-command probe.",
                "status": "open",
                "owner": "",
                "claim_expires": "",
                "depends_on": [],
                "task_revision": 1,
                "updated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
                "spec_ref": reference,
                "spec_revision": 1,
                "branch": f"benchmark/{task_id.lower()}",
                "worktree_key": f"benchmark-{task_id.lower()}",
            }
        )
        item.pop("plan", None)
        item.pop("checkpoint_commit", None)
        (state / "tasks" / f"{task_id}.md").write_text(
            "---\n" + json.dumps(item, indent=2, sort_keys=True) + "\n---\n\n"
            "Disposable benchmark task; never publish this fixture.\n"
        )
        task_spec = dict(spec)
        task_spec["spec_ref"] = reference
        (state / reference).write_text(json.dumps(task_spec, sort_keys=True) + "\n")
    subprocess.run(
        ["git", "add", "--", "tasks", "specs"],  # noqa: S607
        cwd=state,
        env=env,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-S", "-m", "fixture: add disposable concurrency tasks"],  # noqa: S607
        cwd=state,
        env=env,
        check=True,
    )
    command = subprocess.run(
        [sys.executable, "tools/handoffctl.py", "reconcile", "--commit"],
        cwd=state,
        env=env,
        check=False,
        capture_output=True,
        timeout=180,
    )
    if command.returncode:
        raise RuntimeError(
            "fixture reconcile rejected synthetic tasks: " + command.stderr[:500].decode()
        )


def wait_batch(
    running: list[tuple[subprocess.Popen[bytes], float, list[str], Path, Path]],
    started: float,
    name: str,
    timeout: float,
) -> dict[int, float]:
    ended: dict[int, float] = {}
    while len(ended) < len(running):
        if time.monotonic() - started > timeout:
            for process, _, _, _, _ in running:
                if process.poll() is None:
                    process.kill()
            for process, _, _, _, _ in running:
                process.wait()
            raise subprocess.TimeoutExpired(name, timeout)
        for process, _, _, _, _ in running:
            if process.pid not in ended and process.poll() is not None:
                process.wait()
                ended[process.pid] = time.monotonic()
        time.sleep(0.005)
    return ended


def error_class(diagnostic: bytes) -> str:
    if b"LOCK_TIMEOUT" in diagnostic:
        return "lock_timeout"
    if b" is owned by " in diagnostic or b"task claim does not match owner" in diagnostic:
        return "wrong_owner"
    if b"stale revision:" in diagnostic:
        return "stale_task_revision"
    if b"--before must use REF=DIGEST" in diagnostic:
        return "malformed_gate_artifact"
    if b"usage:" in diagnostic:
        return "usage_error"
    if b"requires the SQLite authority" in diagnostic:
        return "unsupported_git_route"
    if b"stale role revision" in diagnostic:
        return "stale_role_revision"
    if b"role" in diagnostic.lower():
        return "role_rejected"
    return "other_error" if diagnostic else "none"


def route_name(argv: list[str]) -> str:
    if argv[:2] == ["doctor", "--live"]:
        return "doctor-live"
    if argv[:2] == ["render-status", "--check"]:
        return "render-check"
    if argv[0] in {"roles", "directive"}:
        return "-".join(argv[:2])
    return argv[0]


def batch(
    state: Path,
    env: dict[str, str],
    name: str,
    commands: list[list[str]],
    timeout: float,
    expected_success: set[int] | None = None,
    expected_errors: dict[int, str] | None = None,
) -> dict[str, object]:
    if len(commands) != 16:
        raise ValueError("every batch must have exactly 16 simultaneous commands")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="awc-16x-output-") as directory:
        root = Path(directory)
        trace = root / "lock-trace.jsonl"
        worker_env = dict(env)
        worker_env["HANDOFFCTL_LOCK_TRACE"] = str(trace)
        running: list[tuple[subprocess.Popen[bytes], float, list[str], Path, Path]] = []
        with contextlib.ExitStack() as handles:
            for index, argv in enumerate(commands):
                stdout_path = root / f"{index}.out"
                stderr_path = root / f"{index}.err"
                stdout = handles.enter_context(stdout_path.open("wb"))
                stderr = handles.enter_context(stderr_path.open("wb"))
                process = subprocess.Popen(  # noqa: S603
                    [sys.executable, "tools/handoffctl.py", *argv],
                    cwd=state,
                    env=worker_env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                )
                running.append((process, time.monotonic(), argv, stdout_path, stderr_path))
            ended = wait_batch(running, started, name, timeout)
        results: list[dict[str, object]] = []
        for index, (process, launch, argv, stdout_path, stderr_path) in enumerate(running):
            with stdout_path.open("rb") as output:
                output_digest = hashlib.file_digest(output, "sha256").hexdigest()
            with stderr_path.open("rb") as diagnostic:
                diagnostic_bytes = diagnostic.read(1_000_000)
            results.append(
                {
                    "index": index,
                    "command": route_name(argv),
                    "exit": process.returncode,
                    "stdout_sha256": output_digest,
                    "stderr_class": error_class(diagnostic_bytes),
                    "elapsed_ms": round((ended[process.pid] - launch) * 1000, 1),
                }
            )
        trace_lines = trace.read_text().splitlines() if trace.exists() else []
    grouped: dict[str, list[dict[str, object]]] = {}
    for result in results:
        grouped.setdefault(str(result["command"]), []).append(result)
    routes = {}
    for command, entries in grouped.items():
        routes[command] = {
            "ok": sum(entry["exit"] == 0 for entry in entries),
            "errors": dict(
                Counter(str(entry["stderr_class"]) for entry in entries if entry["exit"])
            ),
            "max_ms": max(cast(float, entry["elapsed_ms"]) for entry in entries),
            "distinct_stdout": len({str(entry["stdout_sha256"]) for entry in entries}),
        }
    lock_trace: dict[str, dict[str, float | int]] = {}
    for line in trace_lines:
        event = json.loads(line)
        phase = str(event["phase"])
        aggregate = lock_trace.setdefault(
            phase, {"count": 0, "timeouts": 0, "max_wait_ms": 0.0, "max_hold_ms": 0.0}
        )
        aggregate["count"] += 1
        aggregate["timeouts"] += int(event["timeout"])
        aggregate["max_wait_ms"] = max(aggregate["max_wait_ms"], event["wait_ms"])
        aggregate["max_hold_ms"] = max(aggregate["max_hold_ms"], event["hold_ms"] or 0.0)
    report: dict[str, object] = {
        "batch": name,
        "workers": 16,
        "wall_ms": round((time.monotonic() - started) * 1000, 1),
        "routes": routes,
        "lock_trace": lock_trace,
    }
    if expected_success is not None:
        report["expected_outcomes_match"] = all(
            (entry["exit"] == 0) == (entry["index"] in expected_success) for entry in results
        )
        report["good_latency_ms"] = max(
            cast(float, entry["elapsed_ms"])
            for entry in results
            if entry["index"] in expected_success
        )
    if expected_errors is not None:
        report["expected_errors_match"] = all(
            entry["stderr_class"] == expected_errors[index]
            for index, entry in enumerate(results)
            if index in expected_errors
        ) and len(expected_errors) == len(results) - len(expected_success or set())
    return report


def doctor(state: Path, env: dict[str, str]) -> dict[str, object]:
    outcomes: dict[str, object] = {}
    for name, argv in (("doctor", ["doctor"]), ("doctor_live", ["doctor", "--live"])):
        result = subprocess.run(  # noqa: S603
            [sys.executable, "tools/handoffctl.py", *argv],
            cwd=state,
            env=env,
            check=False,
            capture_output=True,
            timeout=180,
        )
        outcomes[name] = result.returncode
    return outcomes


def claimed_mutations(state: Path) -> list[list[str]]:
    """Use disjoint task ownership while readers overlap accepted writes."""
    commands = [["heartbeat", TASK_IDS[index], "--owner", f"bench-{index}"] for index in range(4)]
    commands.extend(
        [
            [
                "update",
                TASK_IDS[index],
                "--owner",
                f"bench-{index}",
                "--expected-revision",
                str(task_meta(state, TASK_IDS[index])["task_revision"]),
                "--note",
                "Disposable concurrent update.",
            ]
            for index in range(4, 8)
        ]
    )
    commands.extend(
        [
            ["run", "--owner", f"bench-{index}", TASK_IDS[index], "--", "/usr/bin/true"]
            for index in range(8, 12)
        ]
    )
    commands.extend([["reconcile"]] * 4)
    return commands


def adversarial_commands(revision: int) -> list[list[str]]:
    """Fifteen rejected authority attempts race one valid owner update."""
    task = TASK_IDS[0]
    bad_owner = "malicious-benchmark-owner"
    commands = [["heartbeat", task, "--owner", bad_owner] for _ in range(3)]
    commands.extend(
        [
            "update",
            task,
            "--owner",
            bad_owner,
            "--expected-revision",
            str(revision),
            "--note",
            "Wrong-owner update must fail.",
        ]
        for _ in range(3)
    )
    commands.extend(
        ["release", task, "--owner", bad_owner, "--status", "open", "--note", "Wrong owner."]
        for _ in range(3)
    )
    commands.extend(
        [
            "update",
            task,
            "--owner",
            "bench-0",
            "--expected-revision",
            str(revision - 1),
            "--note",
            "Stale revision must fail.",
        ]
        for _ in range(2)
    )
    commands.extend(["run", "--owner", bad_owner, task, "--", "/usr/bin/true"] for _ in range(2))
    commands.extend(
        [
            "gate",
            task,
            "--expected-revision",
            str(revision),
            "--stage",
            "intake",
            "--action",
            "open",
            "--disposition",
            "accepted",
            "--before",
            "not-a-digest",
            "--after",
            "artifact=sha256:" + "a" * 64,
            "--public-ref",
            task,
        ]
        for _ in range(2)
    )
    commands.append(
        [
            "update",
            task,
            "--owner",
            "bench-0",
            "--expected-revision",
            str(revision),
            "--note",
            "Adversarial liveness witness.",
        ]
    )
    return commands


def adversarial_probe(state: Path, env: dict[str, str], timeout: float) -> dict[str, object]:
    """Require one valid durable transition amid rejected competing calls."""
    before = task_meta(state, TASK_IDS[0])
    task_path = state / "tasks" / f"{TASK_IDS[0]}.md"
    before_text = task_path.read_text()
    head = git_head(state)
    result = batch(
        state,
        env,
        "adversarial_one_good",
        adversarial_commands(int(before["task_revision"])),
        timeout,
        expected_success={15},
        expected_errors={
            **dict.fromkeys(range(9), "wrong_owner"),
            **dict.fromkeys(range(9, 11), "stale_task_revision"),
            **dict.fromkeys(range(11, 13), "wrong_owner"),
            **dict.fromkeys(range(13, 15), "malformed_gate_artifact"),
        },
    )
    after = task_meta(state, TASK_IDS[0])
    after_text = task_path.read_text()
    commit_count = int(
        subprocess.run(  # noqa: S603
            ["git", "rev-list", "--count", f"{head}..HEAD"],  # noqa: S607
            cwd=state,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    checks = doctor(state, env)
    result["integrity"] = checks
    result["exactly_one_commit"] = commit_count == 1
    result["task_revision_advanced_once"] = after["task_revision"] == before["task_revision"] + 1
    result["owner_preserved"] = after["owner"] == before["owner"] == "bench-0"
    result["good_update_recorded_once"] = (
        before_text.count("Adversarial liveness witness.") == 0
        and after_text.count("Adversarial liveness witness.") == 1
    )
    result["good_worker_live"] = (
        bool(result["expected_outcomes_match"])
        and bool(result["expected_errors_match"])
        and bool(result["good_update_recorded_once"])
    ) and checks == {
        "doctor": 0,
        "doctor_live": 0,
    }
    return result


def releases() -> list[list[str]]:
    return [
        [
            "release",
            task_id,
            "--owner",
            f"bench-{index}",
            "--status",
            "open",
            "--note",
            "Disposable concurrent release.",
        ]
        for index, task_id in enumerate(TASK_IDS)
    ]


def rejected_and_readers() -> list[list[str]]:
    """Unsupported Git routes and absent role assignments must reject explicitly."""
    return (
        [["board"]] * 4
        + [["metrics"]] * 4
        + [["roles", "check", "--owner-id", "missing-benchmark-owner"]] * 4
        + [["directive", "list"]] * 4
    )


def directive_creates() -> list[list[str]]:
    return [
        [
            "directive",
            "create",
            "--directive-id",
            f"UD-{9000 + index:04d}",
            "--authority",
            "BOARD-001",
            "--precedence",
            "100",
            "--task-scope",
            task_id,
            "--statement",
            "Disposable concurrency guidance.",
            "--owner",
            f"bench-board-{index}",
        ]
        for index, task_id in enumerate(TASK_IDS)
    ]


def directive_activations() -> list[list[str]]:
    return [
        [
            "directive",
            "transition",
            f"UD-{9000 + index:04d}",
            "activate",
            "--owner",
            f"bench-board-{index}",
            "--expected-revision",
            "1",
        ]
        for index in range(16)
    ]


def role_assignment(index: int, owner: str, revision: int, expiry: str) -> list[str]:
    return [
        "roles",
        "assign",
        "--expected-revision",
        str(revision),
        "--assignment-id",
        f"RA-{9000 + index}",
        "--owner-id",
        owner,
        "--role-id",
        "implementer",
        "--expires-at",
        expiry,
        "--evidence-kind",
        "ticket",
        "--evidence-ref",
        TASK_IDS[index % 16],
        "--evidence-digest",
        "sha256:" + "a" * 64,
    ]


def seed_active_roles(state: Path, env: dict[str, str]) -> None:
    """Keep the pre-existing active task admitted once the role store exists."""
    owners = sorted(
        {
            str(meta["owner"])
            for path in (state / "tasks").glob("AR-*.md")
            if (meta := json.loads(path.read_text().split("---", 2)[1])).get("status")
            == "in_progress"
            and meta.get("owner")
        }
    )
    if not owners:
        owners = ["bench-role-baseline"]
    expiry = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=2)).replace(microsecond=0).isoformat()
    for revision, owner in enumerate(owners):
        command = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "tools/handoffctl.py",
                *role_assignment(100 + revision, owner, revision, expiry),
            ],
            cwd=state,
            env=env,
            check=False,
            capture_output=True,
            timeout=60,
        )
        if command.returncode:
            raise RuntimeError("active fixture role setup failed: " + command.stderr[:500].decode())


def role_assignments(state: Path) -> list[list[str]]:
    revision = json.loads((state / ".runtime/roles.json").read_text())["revision"]
    expiry = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=2)).replace(microsecond=0).isoformat()
    return [role_assignment(index, f"bench-role-{index}", revision, expiry) for index in range(16)]


def role_reads(state: Path) -> list[list[str]]:
    role_state = json.loads((state / ".runtime/roles.json").read_text())
    owner = next(
        str(item["owner_id"])
        for item in role_state["assignments"]
        if str(item["assignment_id"]).startswith("RA-90")
    )
    return [["roles", "list"]] * 8 + [["roles", "check", "--owner-id", owner]] * 8


def role_removals(state: Path) -> list[list[str]]:
    role_state = json.loads((state / ".runtime/roles.json").read_text())
    assignment = next(
        str(item["assignment_id"])
        for item in role_state["assignments"]
        if str(item["assignment_id"]).startswith("RA-90")
    )
    return [
        [
            "roles",
            "remove",
            "--expected-revision",
            str(role_state["revision"]),
            "--assignment-id",
            assignment,
        ]
        for _ in range(16)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--product", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--only-mutations", action="store_true")
    parser.add_argument("--only-adversarial", action="store_true")
    parser.add_argument("--only-roles", action="store_true")
    args = parser.parse_args()
    source_state = args.state.resolve()
    source_product = args.product.resolve()
    state_head = git_head(source_state)
    product_digest = product_input_digest(source_product)
    binding = json.loads((source_state / "coordinator.binding.json").read_text())
    with tempfile.TemporaryDirectory(prefix="awc-16x-mixed-") as directory:
        root = Path(directory)
        runtime = prepare_runtime(root, git_head(Path(__file__).resolve().parents[1]), "candidate")
        state, _, env = prepare(
            root,
            source_state,
            source_product,
            "candidate",
            True,
            runtime_source=runtime,
            state_commit=state_head,
        )
        configure(state, source_product, str(binding["product_repository"]), env)
        add_fixture_tasks(state, env)
        print(
            json.dumps(
                {
                    "source_state": state_head,
                    "candidate": git_head(runtime),
                    "fixture_tasks": len(TASK_IDS),
                    "product_worktrees": worktree_listing(source_product).count("worktree "),
                }
            ),
            flush=True,
        )
        initial_batches = (
            (
                "reads",
                [["doctor"]] * 4
                + [["doctor", "--live"]] * 4
                + [["snapshot"]] * 4
                + [["render-status", "--check"]] * 4,
            ),
            (
                "live_mixed",
                [["doctor", "--live"]] * 4
                + [["snapshot"]] * 4
                + [["reconcile"]] * 4
                + [["render-status", "--check"]] * 4,
            ),
            (
                "claims",
                [
                    ["claim", task_id, "--owner", f"bench-{index}"]
                    for index, task_id in enumerate(TASK_IDS)
                ],
            ),
        )
        batches = (
            ()
            if args.only_roles
            else initial_batches[-1:]
            if args.only_mutations or args.only_adversarial
            else initial_batches
        )
        for name, commands in batches:
            print(json.dumps(batch(state, env, name, commands, args.timeout_seconds)), flush=True)
            print(json.dumps({"after": name, "integrity": doctor(state, env)}), flush=True)
        if not args.only_roles:
            adversarial = adversarial_probe(state, env, args.timeout_seconds)
            print(json.dumps(adversarial), flush=True)
            if not (
                adversarial["good_worker_live"]
                and adversarial["exactly_one_commit"]
                and adversarial["task_revision_advanced_once"]
                and adversarial["owner_preserved"]
                and adversarial["good_update_recorded_once"]
            ):
                raise RuntimeError("adversarial concurrency integrity or liveness failure")
        if args.only_adversarial:
            print(
                json.dumps(
                    {
                        "source_product_inputs_changed": product_input_digest(source_product)
                        != product_digest,
                        "source_state_head_changed": git_head(source_state) != state_head,
                    }
                ),
                flush=True,
            )
            return
        mutation_batches = (
            ()
            if args.only_roles
            else (
                ("claimed_mutations", claimed_mutations(state)),
                ("releases", releases()),
                ("rejected_and_readers", rejected_and_readers()),
                ("directive_creates", directive_creates()),
                ("directive_activations", directive_activations()),
            )
        )
        for name, commands in mutation_batches:
            print(json.dumps(batch(state, env, name, commands, args.timeout_seconds)), flush=True)
            print(json.dumps({"after": name, "integrity": doctor(state, env)}), flush=True)
        seed_active_roles(state, env)
        print(json.dumps({"after": "role_seed", "integrity": doctor(state, env)}), flush=True)
        print(
            json.dumps(
                batch(state, env, "role_assignments", role_assignments(state), args.timeout_seconds)
            ),
            flush=True,
        )
        print(
            json.dumps({"after": "role_assignments", "integrity": doctor(state, env)}), flush=True
        )
        for name, commands in (
            ("role_reads", role_reads(state)),
            ("role_removals", role_removals(state)),
        ):
            print(json.dumps(batch(state, env, name, commands, args.timeout_seconds)), flush=True)
            print(json.dumps({"after": name, "integrity": doctor(state, env)}), flush=True)
        print(
            json.dumps(
                {
                    "source_product_inputs_changed": product_input_digest(source_product)
                    != product_digest,
                    "source_state_head_changed": git_head(source_state) != state_head,
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
