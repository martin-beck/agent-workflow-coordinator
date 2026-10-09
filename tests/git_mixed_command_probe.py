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
import math
import os
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, cast

from git_command_latency_benchmark import (
    configure,
    dirty_checkout_digest,
    git_head,
    history_extends,
    non_head_refs_digest,
    prepare_runtime,
    product_input_digest,
    runtime_digest,
    worktree_listing,
)
from git_scale_probe import prepare

from tools.session_records import build_session_record

TASK_IDS = tuple(f"AR-{number:04d}" for number in range(9000, 9016))


def stabilize_disposable_claims(state: Path) -> int:
    """Keep a pinned fixture usable after its real-world claim leases expire.

    Only the disposable clone is changed. The signed normalization commit is
    made by the subsequent fixture setup reconcile before any measured batch,
    so every worker sees the same authority state and the source stays untouched.
    """
    current_time = dt.datetime.now(dt.UTC)
    deadline = (current_time + dt.timedelta(days=1)).replace(microsecond=0).isoformat()
    rewrites: list[tuple[Path, str]] = []
    for path in sorted((state / "tasks").glob("*.md")):
        original = path.read_text()
        front, metadata, body = original.split("---", 2)
        if front.strip():
            raise RuntimeError(f"unexpected task front matter: {path}")
        item = json.loads(metadata)
        if item.get("status") != "in_progress" or not item.get("claim_expires"):
            continue
        try:
            expires = dt.datetime.fromisoformat(item["claim_expires"])
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"invalid source claim expiry: {path}") from error
        if expires.tzinfo is None or expires.utcoffset() is None:
            raise RuntimeError(f"source claim expiry has no timezone: {path}")
        if expires > current_time:
            continue
        item["claim_expires"] = deadline
        rewrites.append(
            (path, "---\n" + json.dumps(item, indent=2, sort_keys=True) + "\n---" + body)
        )
    for path, replacement in rewrites:
        path.write_text(replacement)
    return len(rewrites)


def task_meta(state: Path, task_id: str) -> dict[str, Any]:
    path = state / "tasks" / f"{task_id}.md"
    return cast(dict[str, Any], json.loads(path.read_text().split("---", 2)[1]))


def acceptances(state: Path) -> list[list[str]]:
    """Give each claimed fixture task one independent spec-acceptance write."""
    return [
        [
            "accept",
            task_id,
            "--owner",
            f"bench-{index}",
            "--expected-revision",
            str(task_meta(state, task_id)["task_revision"]),
            "--evidence-class",
            "mechanical",
            "--evidence-ref",
            f"quality/{task_id}",
            "--evidence-digest",
            "sha256:" + "a" * 64,
            "--note",
            "Disposable concurrency acceptance.",
        ]
        for index, task_id in enumerate(TASK_IDS)
    ]


def acceptance_unchanged_fields(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in meta.items()
        if key not in {"task_revision", "updated_at", "spec_acceptance"}
    }


def heartbeat_effect_errors(  # noqa: C901
    state: Path,
    before: dict[str, dict[str, Any]],
    bodies: dict[str, str],
    started_at: dt.datetime,
    finished_at: dt.datetime,
) -> list[str]:
    """Bind every successful route to the exact requested heartbeat effect."""
    errors: list[str] = []
    for index, task_id in enumerate(TASK_IDS):
        after = task_meta(state, task_id)
        original = before[task_id]
        if after.get("owner") != f"bench-{index}" or after.get("status") != "in_progress":
            errors.append(f"{task_id}: owner or status changed")
        if after.get("task_revision") != original["task_revision"] + 1:
            errors.append(f"{task_id}: revision did not advance once")

        def unchanged(item: dict[str, Any]) -> dict[str, Any]:
            return {
                key: value
                for key, value in item.items()
                if key not in {"task_revision", "updated_at", "claim_expires"}
            }

        if unchanged(after) != unchanged(original):
            errors.append(f"{task_id}: unrelated metadata changed")
        try:
            expiry = dt.datetime.fromisoformat(after["claim_expires"])
            updated = dt.datetime.fromisoformat(after["updated_at"])
        except (KeyError, TypeError, ValueError) as error:
            errors.append(f"{task_id}: invalid heartbeat timestamp: {error}")
            continue
        if not (
            started_at + dt.timedelta(minutes=20, seconds=-2)
            <= expiry
            <= finished_at + dt.timedelta(minutes=20, seconds=2)
        ):
            errors.append(f"{task_id}: lease does not match requested 20 minutes")
        if (
            not started_at - dt.timedelta(seconds=2)
            <= updated
            <= finished_at + dt.timedelta(seconds=2)
        ):
            errors.append(f"{task_id}: updated_at is outside the operation window")
        if abs((expiry - updated - dt.timedelta(minutes=20)).total_seconds()) > 1:
            errors.append(f"{task_id}: lease duration differs from requested 20 minutes")
        expected_body = (
            bodies[task_id] + f"\n- {after['updated_at']}: Heartbeat by bench-{index}.\n"
        )
        if (state / "tasks" / f"{task_id}.md").read_text().split("---", 2)[2] != expected_body:
            errors.append(f"{task_id}: heartbeat history/body does not match request")
    return errors


def coordinator_private_root(state: Path) -> Path:
    """Resolve the disposable clone's private Git-common-directory state."""
    common_name = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],  # noqa: S607
        cwd=state,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    common = Path(common_name)
    if not common.is_absolute():
        common = state / common
    return common / "handoffctl"


def coordinator_other_digest(state: Path) -> bytes:
    """Fence unexpected private Git-common-directory effects beyond receipt files."""
    root = coordinator_private_root(state)
    allowed = {
        "state.lock",
        "fast-receipts.sqlite3",
        "fast-receipts.sqlite3-wal",
        "fast-receipts.sqlite3-shm",
        "fast-receipts.service.lock",
        "fast-receipts.publication.lock",
    }
    digest = hashlib.sha256()
    if root.exists():
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if relative.parts[0] in allowed:
                if len(relative.parts) != 1 or not path.is_file() or path.is_symlink():
                    raise RuntimeError("receipt path has unexpected shape")
                continue
            digest.update(os.fsencode(str(relative)) + b"\0")
            if path.is_symlink():
                digest.update(b"symlink\0" + os.fsencode(path.readlink()))
            elif path.is_file():
                digest.update(path.read_bytes())
            else:
                digest.update(b"directory\0")
    return digest.digest()


def receipt_queue_errors(state: Path, completed: dict[str, dict[str, object]]) -> list[str]:
    """Reject extra intents and bind every durable row to one requested heartbeat."""
    path = coordinator_private_root(state) / "fast-receipts.sqlite3"
    if not path.is_file() or path.is_symlink():
        return ["fast receipt database is absent or unsafe"]
    project_id = json.loads((state / "coordinator.binding.json").read_text())["project_id"]
    with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        binding = connection.execute(
            "SELECT project_id FROM receipt_binding WHERE singleton=1"
        ).fetchone()
        rows = connection.execute("SELECT * FROM intents").fetchall()
    errors: list[str] = []
    if binding is None or binding["project_id"] != project_id:
        errors.append("fast receipt database has wrong project binding")
    if len(rows) != len(TASK_IDS) or {row["receipt_id"] for row in rows} != set(completed):
        errors.append("fast receipt database has missing or extra durable intents")
    for row in rows:
        task_id = str(row["task_id"])
        if task_id not in TASK_IDS:
            errors.append("fast receipt database has unexpected task")
            continue
        index = TASK_IDS.index(task_id)
        payload = {
            "expected_revision": 2,
            "lease_minutes": 20,
            "operation": "heartbeat",
            "owner": f"bench-{index}",
            "project_id": project_id,
            "task": task_id,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if (
            row["project_id"] != project_id
            or row["idempotency_key"] != f"bench-{index}:heartbeat:2"
            or row["operation"] != "heartbeat"
            or row["expected_revision"] != 2
            or row["payload_json"] != canonical
            or row["input_digest"] != hashlib.sha256(canonical.encode()).hexdigest()
            or row["phase"] != "completed-local"
            or row["result_revision"] != 3
            or row["error_code"] is not None
            or row["remote_oid"] is not None
        ):
            errors.append(f"{task_id}: durable intent differs from requested heartbeat")
        receipt = completed.get(str(row["receipt_id"]))
        if receipt is None or any(
            row[field] != receipt[field]
            for field in (
                "project_id",
                "operation",
                "task_id",
                "expected_revision",
                "phase",
                "commit_oid",
                "result_revision",
                "error_code",
                "remote_oid",
            )
        ):
            errors.append(f"{task_id}: durable intent does not match public receipt")
    return errors


def heartbeat_commit_errors(  # noqa: C901
    state: Path, starting_head: str, receipt_ids: dict[str, str] | None = None
) -> tuple[int, list[str]]:
    """Require one exact signed, DCO task commit for each requested heartbeat."""
    revisions = subprocess.run(  # noqa: S603
        ["git", "rev-list", f"{starting_head}..HEAD"],  # noqa: S607
        cwd=state,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    errors: list[str] = []
    if len(revisions) != len(TASK_IDS) or not history_extends(state, starting_head):
        errors.append("heartbeat batch did not add 16 extending commits")
    if receipt_ids is not None and set(revisions) != set(receipt_ids):
        errors.append("fast receipt commits do not match exact Git history")
    seen: set[str] = set()
    for commit_hash in revisions:
        commit = subprocess.run(  # noqa: S603
            ["git", "show", "-s", "--format=%P%x00%an%x00%ae%x00%s%x00%B", commit_hash],  # noqa: S607
            cwd=state,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        parents, author_name, author_email, subject, message = commit.split("\0", 4)
        if len(parents.split()) != 1:
            errors.append(f"{commit_hash}: heartbeat commit is not single-parent")
        prefix = "chore(state): heartbeat "
        task_id = subject.removeprefix(prefix)
        if not subject.startswith(prefix) or task_id not in TASK_IDS or task_id in seen:
            errors.append(f"{commit_hash}: heartbeat commit subject or task is invalid")
        else:
            seen.add(task_id)
            paths = subprocess.run(  # noqa: S603
                ["git", "diff-tree", "-r", "--no-commit-id", "--name-only", "-z", commit_hash],  # noqa: S607
                cwd=state,
                check=True,
                capture_output=True,
            ).stdout.split(b"\0")
            if [path for path in paths if path] != [f"tasks/{task_id}.md".encode()]:
                errors.append(f"{commit_hash}: unrelated committed path changed")
        if subprocess.run(  # noqa: S603
            ["git", "verify-commit", commit_hash],  # noqa: S607
            cwd=state,
            check=False,
            capture_output=True,
        ).returncode:
            errors.append(f"{commit_hash}: signature did not verify")
        if (author_name, author_email) != ("Fixture", "fixture@example.invalid") or not (
            has_matching_dco_trailer(message, author_name, author_email)
        ):
            errors.append(f"{commit_hash}: matching author/DCO trailer missing")
        marker = (
            f"Handoffctl-Receipt: {receipt_ids[commit_hash]}"
            if receipt_ids and commit_hash in receipt_ids
            else None
        )
        if receipt_ids is not None and (marker is None or message.splitlines().count(marker) != 1):
            errors.append(f"{commit_hash}: matching receipt marker missing")
        if receipt_ids is None and any(
            line.startswith("Handoffctl-Receipt: ") for line in message.splitlines()
        ):
            errors.append(f"{commit_hash}: strict heartbeat has unexpected receipt marker")
    if seen != set(TASK_IDS):
        errors.append("heartbeat batch did not commit each task exactly once")
    return len(revisions), errors


def acceptance_errors(state: Path, before: dict[str, dict[str, Any]]) -> list[str]:
    """Reject a success-only result if any strict acceptance was not recorded."""
    errors: list[str] = []
    for index, task_id in enumerate(TASK_IDS):
        meta = task_meta(state, task_id)
        original = before[task_id]
        acceptance = meta.get("spec_acceptance")
        expected = {
            "spec_ref": original.get("spec_ref"),
            "spec_revision": original.get("spec_revision"),
            "status": "pass",
            "evidence_class": "mechanical",
            "evidence_ref": f"quality/{task_id}",
            "evidence_digest": "sha256:" + "a" * 64,
        }
        if meta.get("owner") != f"bench-{index}":
            errors.append(f"{task_id}: owner changed")
        if meta.get("task_revision") != original["task_revision"] + 1:
            errors.append(f"{task_id}: revision did not advance once")
        if acceptance != expected:
            errors.append(f"{task_id}: acceptance does not match request")
        if acceptance_unchanged_fields(meta) != acceptance_unchanged_fields(original):
            errors.append(f"{task_id}: unrelated task fields changed")
        if "spec_acceptance" in original:
            errors.append(f"{task_id}: acceptance existed before request")
    return errors


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
        durations = sorted(cast(float, entry["elapsed_ms"]) for entry in entries)
        routes[command] = {
            "ok": sum(entry["exit"] == 0 for entry in entries),
            "errors": dict(
                Counter(str(entry["stderr_class"]) for entry in entries if entry["exit"])
            ),
            "p50_ms": round(statistics.median(durations), 1),
            "p95_ms": durations[math.ceil(0.95 * len(durations)) - 1],
            "max_ms": durations[-1],
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


def adversarial_commands(revision: int, marker: Path) -> list[list[str]]:
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
    unauthorized_effect = (
        f"from pathlib import Path; Path({str(marker)!r}).write_text('unauthorized')"
    )
    commands.extend(
        ["run", "--owner", bad_owner, task, "--", sys.executable, "-c", unauthorized_effect]
        for _ in range(2)
    )
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
    session_path = state / "sessions" / f"{TASK_IDS[0]}.jsonl"
    before_text = task_path.read_text()
    before_sessions = session_path.read_text().splitlines() if session_path.exists() else []
    head = git_head(state)
    before_dirty = dirty_checkout_digest(state)
    before_runtime = runtime_digest(state, normalize_time=False)
    before_refs = non_head_refs_digest(state)
    marker = state.parent / "unauthorized-run-marker"
    result = batch(
        state,
        env,
        "adversarial_one_good",
        adversarial_commands(int(before["task_revision"]), marker),
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
    after_sessions = session_path.read_text().splitlines() if session_path.exists() else []
    commit_count = int(
        subprocess.run(  # noqa: S603
            ["git", "rev-list", "--count", f"{head}..HEAD"],  # noqa: S607
            cwd=state,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    after_dirty = dirty_checkout_digest(state)
    after_runtime = runtime_digest(state, normalize_time=False)
    after_refs = non_head_refs_digest(state)
    changed_paths = subprocess.run(  # noqa: S603
        ["/usr/bin/git", "-C", str(state), "diff", "--name-only", "-z", f"{head}..HEAD"],
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    allowed_paths = {
        b"tasks/AR-9000.md",
        b"sessions/AR-9000.jsonl",
        b"CURRENT.md",
        b"STATUS.md",
    }
    result["only_expected_durable_paths_changed"] = all(
        not path or path in allowed_paths or path.startswith(b"status/") for path in changed_paths
    )
    result["no_uncommitted_effects"] = before_dirty == after_dirty
    result["runtime_unchanged"] = before_runtime == after_runtime
    result["non_head_refs_unchanged"] = before_refs == after_refs
    checks = doctor(state, env)
    result["integrity"] = checks
    result["exactly_one_commit"] = commit_count == 1
    result["starting_history_preserved"] = history_extends(state, head)
    result["task_revision_advanced_once"] = after["task_revision"] == before["task_revision"] + 1
    result["one_expected_session_record"] = (
        len(after_sessions) == len(before_sessions) + 1
        and after_sessions[: len(before_sessions)] == before_sessions
        and json.loads(after_sessions[-1])
        == build_session_record(after, "update", str(after["updated_at"]))
    )
    result["owner_preserved"] = after["owner"] == before["owner"] == "bench-0"
    result["good_update_recorded_once"] = (
        before_text.count("Adversarial liveness witness.") == 0
        and after_text.count("Adversarial liveness witness.") == 1
    )
    result["unauthorized_subprocess_suppressed"] = not marker.exists()
    result["good_latency_under_10s"] = cast(float, result["good_latency_ms"]) < 10_000
    result["good_worker_live"] = (
        bool(result["expected_outcomes_match"])
        and bool(result["expected_errors_match"])
        and bool(result["good_update_recorded_once"])
        and bool(result["good_latency_under_10s"])
        and bool(result["only_expected_durable_paths_changed"])
        and bool(result["no_uncommitted_effects"])
        and bool(result["runtime_unchanged"])
        and bool(result["non_head_refs_unchanged"])
        and bool(result["one_expected_session_record"])
        and bool(result["starting_history_preserved"])
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


def require_unchanged_sources(report: dict[str, bool]) -> None:
    print(json.dumps(report), flush=True)
    if any(report.values()):
        raise RuntimeError("concurrency qualification invalid: source inputs changed")


def checked_batch(
    state: Path,
    env: dict[str, str],
    name: str,
    commands: list[list[str]],
    timeout: float,
    expected_successes: int,
) -> None:
    outcome = batch(state, env, name, commands, timeout)
    print(json.dumps(outcome), flush=True)
    checks = doctor(state, env)
    print(json.dumps({"after": name, "integrity": checks}), flush=True)
    successes = sum(
        cast(int, route["ok"])
        for route in cast(dict[str, dict[str, object]], outcome["routes"]).values()
    )
    if successes != expected_successes or any(checks.values()):
        raise RuntimeError(f"{name}: unexpected route outcome or failed integrity check")


def run_fast_heartbeat_probe(  # noqa: C901
    state: Path, env: dict[str, str], timeout: float
) -> None:
    """Exercise 16 queued CLI submissions against one resident signed-Git executor."""
    configure_fixture_signature_verification(state)
    before = {task_id: task_meta(state, task_id) for task_id in TASK_IDS}
    before_bodies = {
        task_id: (state / "tasks" / f"{task_id}.md").read_text().split("---", 2)[2]
        for task_id in TASK_IDS
    }
    starting_head = git_head(state)
    before_dirty = dirty_checkout_digest(state)
    before_runtime = runtime_digest(state, normalize_time=False)
    before_refs = non_head_refs_digest(state)
    before_private = coordinator_other_digest(state)
    started_at = dt.datetime.now(dt.UTC)
    service_started = time.monotonic()
    service = subprocess.Popen(
        [sys.executable, "tools/handoffctl.py", "fast", "worker", "--serve"],
        cwd=state,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    launched: list[tuple[subprocess.Popen[bytes], float]] = []
    try:
        socket_path = coordinator_private_root(state) / "fast-receipts.sock"
        ready_deadline = time.monotonic() + timeout
        while True:
            if service.poll() is not None:
                diagnostic = service.stderr.read(500) if service.stderr else b""
                raise RuntimeError(
                    "resident fast worker exited before socket readiness: "
                    + diagnostic.decode(errors="replace")
                )
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.2)
                    probe.connect(str(socket_path))
                break
            except (FileNotFoundError, ConnectionRefusedError) as error:
                if time.monotonic() >= ready_deadline:
                    raise RuntimeError("resident fast socket readiness timeout") from error
                time.sleep(0.01)
        service_ready_ms = (time.monotonic() - service_started) * 1000
        batch_started = time.monotonic()
        socket_required_env = {**env, "HANDOFFCTL_FAST_REQUIRE_SOCKET": "1"}
        for index, task_id in enumerate(TASK_IDS):
            command = [
                sys.executable,
                "tools/handoffctl.py",
                "fast",
                "heartbeat",
                task_id,
                "--owner",
                f"bench-{index}",
                "--expected-revision",
                "2",
                "--lease-minutes",
                "20",
                "--key",
                f"bench-{index}:heartbeat:2",
            ]
            launched.append(
                (
                    subprocess.Popen(  # noqa: S603
                        command,
                        cwd=state,
                        env=socket_required_env,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    ),
                    time.monotonic(),
                )
            )
        queued: list[dict[str, object]] = []
        latencies: list[float] = []
        ended: dict[int, float] = {}
        enqueue_deadline = time.monotonic() + timeout
        while len(ended) < len(launched):
            if time.monotonic() >= enqueue_deadline:
                raise RuntimeError("fast enqueue liveness timeout")
            for process, _ in launched:
                if process.pid not in ended and process.poll() is not None:
                    ended[process.pid] = time.monotonic()
            if len(ended) < len(launched):
                time.sleep(0.005)
        for process, launched_at in launched:
            output, diagnostic = process.communicate(timeout=5)
            latencies.append((ended[process.pid] - launched_at) * 1000)
            if process.returncode:
                raise RuntimeError(
                    f"fast enqueue failed ({process.returncode}): "
                    + diagnostic[:500].decode(errors="replace")
                )
            receipt = json.loads(output)
            if (
                receipt.get("phase") != "queued-local"
                or receipt.get("commit_oid") is not None
                or receipt.get("remote_oid") is not None
                or receipt.get("operation") != "heartbeat"
                or receipt.get("task_id") != TASK_IDS[len(queued)]
                or receipt.get("expected_revision") != 2
            ):
                raise RuntimeError("fast enqueue falsely reported completed authority")
            queued.append(receipt)
        if len({item["receipt_id"] for item in queued}) != 16:
            raise RuntimeError("16 independent fast workers did not get distinct receipts")
        pending = {str(item["receipt_id"]): item for item in queued}
        completed: dict[str, dict[str, object]] = {}
        deadline = time.monotonic() + timeout
        while pending:
            if service.poll() is not None:
                diagnostic = service.stderr.read(500) if service.stderr else b""
                raise RuntimeError(
                    "resident fast worker exited before completing 16 intents: "
                    + diagnostic.decode(errors="replace")
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(f"fast worker liveness timeout: {len(pending)} receipts pending")
            for receipt_id in list(pending):
                read = subprocess.run(  # noqa: S603
                    [sys.executable, "tools/handoffctl.py", "fast", "receipt", receipt_id],
                    cwd=state,
                    env=socket_required_env,
                    check=False,
                    capture_output=True,
                    timeout=30,
                )
                if read.returncode:
                    raise RuntimeError("fast receipt lookup failed: " + read.stderr[:500].decode())
                current = json.loads(read.stdout)
                original = pending[receipt_id]
                if (
                    current.get("receipt_id") != receipt_id
                    or current.get("task_id") != original["task_id"]
                    or current.get("operation") != "heartbeat"
                    or current.get("expected_revision") != 2
                ):
                    raise RuntimeError("fast receipt identity changed after enqueue")
                if current["phase"] == "completed-local":
                    if (
                        current["result_revision"] != 3
                        or not current["commit_oid"]
                        or current["remote_oid"] is not None
                    ):
                        raise RuntimeError("fast local receipt has invalid phase evidence")
                    completed[receipt_id] = current
                    pending.pop(receipt_id)
                elif current["phase"] not in {"queued-local", "running"}:
                    raise RuntimeError(f"fast heartbeat failed: {current['phase']}")
            if pending:
                time.sleep(0.1)
        local_wall_ms = (time.monotonic() - batch_started) * 1000
        finished_at = dt.datetime.now(dt.UTC)
    finally:
        for process, _ in launched:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        if service.poll() is None:
            service.terminate()
        try:
            service.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            service.kill()
            service.communicate()
    by_commit = {str(item["commit_oid"]): item for item in completed.values()}
    if len(by_commit) != len(TASK_IDS) or {
        receipt["task_id"] for receipt in by_commit.values()
    } != set(TASK_IDS):
        raise RuntimeError("fast receipt task/commit mapping is not one-to-one")
    count, errors = heartbeat_commit_errors(
        state,
        starting_head,
        {commit_hash: str(receipt["receipt_id"]) for commit_hash, receipt in by_commit.items()},
    )
    for commit_hash, receipt in by_commit.items():
        subject = subprocess.run(  # noqa: S603
            ["git", "show", "-s", "--format=%s", commit_hash],  # noqa: S607
            cwd=state,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if subject != f"chore(state): heartbeat {receipt['task_id']}":
            errors.append(f"{commit_hash}: receipt task does not match commit task")
    errors.extend(heartbeat_effect_errors(state, before, before_bodies, started_at, finished_at))
    errors.extend(receipt_queue_errors(state, completed))
    if dirty_checkout_digest(state) != before_dirty:
        errors.append("fast heartbeat left unrelated uncommitted state")
    if runtime_digest(state, normalize_time=False) != before_runtime:
        errors.append("fast heartbeat changed ignored runtime state")
    if non_head_refs_digest(state) != before_refs:
        errors.append("fast heartbeat changed non-HEAD Git refs")
    if coordinator_other_digest(state) != before_private:
        errors.append("fast heartbeat changed unrelated private Git state")
    if errors:
        raise RuntimeError("fast heartbeat durability mismatch: " + "; ".join(errors))
    checks = doctor(state, env)
    if any(checks.values()):
        raise RuntimeError("post-fast heartbeat doctors failed")
    print(
        json.dumps(
            {
                "batch": "fast_heartbeat",
                "workers": 16,
                "service_ready_ms": round(service_ready_ms, 1),
                "enqueue_max_ms": round(max(latencies), 1),
                "enqueue_p50_ms": round(statistics.median(latencies), 1),
                "enqueue_p95_ms": round(sorted(latencies)[math.ceil(0.95 * len(latencies)) - 1], 1),
                "local_wall_ms": round(local_wall_ms, 1),
                "completed_local": 16,
                "signed_commits": count,
                "no_unrelated_effects": True,
                "integrity": checks,
            }
        ),
        flush=True,
    )


def fast_hostile_commands() -> list[list[str]]:
    """One valid owner races 15 wrong-owner or stale-revision heartbeats."""
    commands: list[list[str]] = []
    for index in range(16):
        good = index == 8
        owner = "bench-0" if good or index % 2 == 0 else f"mallory-{index}"
        revision = "2" if good or index % 2 else "1"
        commands.append(
            [
                "fast",
                "heartbeat",
                TASK_IDS[0],
                "--owner",
                owner,
                "--expected-revision",
                revision,
                "--lease-minutes",
                "20",
                "--key",
                f"hostile-{index}:heartbeat:2",
            ]
        )
    return commands


def run_fast_hostile_probe(  # noqa: C901
    state: Path, env: dict[str, str], timeout: float
) -> None:
    """Prove one good socket worker progresses amid 15 invalid durable intents."""
    configure_fixture_signature_verification(state)
    task = TASK_IDS[0]
    before = {task_id: task_meta(state, task_id) for task_id in TASK_IDS}
    before_body = (state / "tasks" / f"{task}.md").read_text().split("---", 2)[2]
    starting_head = git_head(state)
    before_dirty = dirty_checkout_digest(state)
    before_runtime = runtime_digest(state, normalize_time=False)
    before_refs = non_head_refs_digest(state)
    before_private = coordinator_other_digest(state)
    started_at = dt.datetime.now(dt.UTC)
    service = subprocess.Popen(
        [sys.executable, "tools/handoffctl.py", "fast", "worker", "--serve"],
        cwd=state,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    socket_path = coordinator_private_root(state) / "fast-receipts.sock"
    required_env = {**env, "HANDOFFCTL_FAST_REQUIRE_SOCKET": "1"}
    running: list[subprocess.Popen[bytes]] = []
    started = time.monotonic()
    receipts: dict[int, dict[str, Any]] = {}
    try:
        while True:
            if service.poll() is not None:
                diagnostic = service.stderr.read(500) if service.stderr else b""
                raise RuntimeError(
                    "fast hostile service exited: " + diagnostic.decode(errors="replace")
                )
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.2)
                    probe.connect(str(socket_path))
                break
            except (FileNotFoundError, ConnectionRefusedError) as error:
                if time.monotonic() - started >= timeout:
                    raise RuntimeError("fast hostile socket readiness timeout") from error
                time.sleep(0.01)
        running.extend(
            subprocess.Popen(  # noqa: S603
                [sys.executable, "tools/handoffctl.py", *command],
                cwd=state,
                env=required_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for command in fast_hostile_commands()
        )
        for index, process in enumerate(running):
            output, diagnostic = process.communicate(timeout=timeout)
            if process.returncode:
                raise RuntimeError(
                    f"fast hostile enqueue {index} failed: "
                    + diagnostic[:500].decode(errors="replace")
                )
            receipt = json.loads(output)
            if (
                receipt.get("phase") != "queued-local"
                or receipt.get("task_id") != task
                or receipt.get("commit_oid") is not None
            ):
                raise RuntimeError("fast hostile enqueue reported non-queued authority")
            receipts[index] = receipt
        if len({item["receipt_id"] for item in receipts.values()}) != 16:
            raise RuntimeError("fast hostile submissions did not create 16 distinct receipts")
        completed: dict[int, dict[str, Any]] = {}
        good_completed_at: float | None = None
        deadline = time.monotonic() + timeout
        while len(completed) < 16:
            if service.poll() is not None:
                raise RuntimeError("fast hostile worker exited before terminal receipts")
            if time.monotonic() >= deadline:
                raise RuntimeError("fast hostile good-worker liveness timeout")
            for index, original in receipts.items():
                if index in completed:
                    continue
                observed = subprocess.run(  # noqa: S603
                    [
                        sys.executable,
                        "tools/handoffctl.py",
                        "fast",
                        "receipt",
                        original["receipt_id"],
                    ],
                    cwd=state,
                    env=required_env,
                    check=False,
                    capture_output=True,
                    timeout=30,
                )
                if observed.returncode:
                    raise RuntimeError(
                        "fast hostile receipt lookup failed: " + observed.stderr[:500].decode()
                    )
                current = json.loads(observed.stdout)
                if current["receipt_id"] != original["receipt_id"] or current["task_id"] != task:
                    raise RuntimeError("fast hostile receipt identity changed")
                if current["phase"] in {"completed-local", "rejected", "ambiguous"}:
                    completed[index] = current
                    if index == 8 and current["phase"] == "completed-local":
                        good_completed_at = time.monotonic()
            if len(completed) < 16:
                time.sleep(0.1)
        if good_completed_at is None:
            raise RuntimeError("fast hostile good worker never completed locally")
        good_local_wall_ms = (good_completed_at - started) * 1000
        finished_at = dt.datetime.now(dt.UTC)
    finally:
        for process in running:
            if process.poll() is None:
                process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.communicate(timeout=5)
            if process.poll() is None:
                process.kill()
                process.communicate()
        if service.poll() is None:
            service.terminate()
        try:
            service.communicate(timeout=10)
        except subprocess.TimeoutExpired as error:
            service.kill()
            service.communicate()
            raise RuntimeError("fast hostile worker did not stop gracefully") from error
    if service.returncode != 0:
        raise RuntimeError("fast hostile worker failed graceful shutdown")
    good = completed[8]
    if (
        good["phase"] != "completed-local"
        or good["result_revision"] != 3
        or not good["commit_oid"]
        or good["remote_oid"] is not None
        or any(completed[index]["phase"] != "rejected" for index in range(16) if index != 8)
    ):
        raise RuntimeError("fast hostile outcome misclassified a good or bad worker")
    commits = subprocess.run(  # noqa: S603
        ["git", "rev-list", f"{starting_head}..HEAD"],  # noqa: S607
        cwd=state,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    if (
        len(commits) != 1
        or commits[0] != good["commit_oid"]
        or not history_extends(state, starting_head)
    ):
        raise RuntimeError("fast hostile batch did not add exactly one extending good commit")
    verified = subprocess.run(  # noqa: S603
        ["git", "verify-commit", commits[0]],  # noqa: S607
        cwd=state,
        check=False,
        capture_output=True,
    )
    commit = subprocess.run(  # noqa: S603
        ["git", "show", "-s", "--format=%P%x00%an%x00%ae%x00%s%x00%B", commits[0]],  # noqa: S607
        cwd=state,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    parents, author_name, author_email, subject, message = commit.split("\0", 4)
    paths = subprocess.run(  # noqa: S603
        ["git", "diff-tree", "-r", "--no-commit-id", "--name-only", "-z", commits[0]],  # noqa: S607
        cwd=state,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    if (
        verified.returncode
        or len(parents.split()) != 1
        or subject != f"chore(state): heartbeat {task}"
        or [path for path in paths if path] != [f"tasks/{task}.md".encode()]
        or (author_name, author_email) != ("Fixture", "fixture@example.invalid")
        or not has_matching_dco_trailer(message, author_name, author_email)
        or message.splitlines().count(f"Handoffctl-Receipt: {good['receipt_id']}") != 1
    ):
        raise RuntimeError("fast hostile good commit lacks exact path, signature, DCO or receipt")
    after = task_meta(state, task)
    after_body = (state / "tasks" / f"{task}.md").read_text().split("---", 2)[2]

    def unchanged(item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in item.items()
            if key not in {"task_revision", "updated_at", "claim_expires"}
        }

    try:
        updated = dt.datetime.fromisoformat(after["updated_at"])
        expiry = dt.datetime.fromisoformat(after["claim_expires"])
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("fast hostile heartbeat timestamps are invalid") from error
    if (
        after["task_revision"] != before[task]["task_revision"] + 1
        or unchanged(after) != unchanged(before[task])
        or not started_at - dt.timedelta(seconds=2)
        <= updated
        <= finished_at + dt.timedelta(seconds=2)
        or abs((expiry - updated - dt.timedelta(minutes=20)).total_seconds()) > 1
        or after_body != before_body + f"\n- {after['updated_at']}: Heartbeat by bench-0.\n"
        or any(task_meta(state, other) != meta for other, meta in before.items() if other != task)
    ):
        raise RuntimeError("fast hostile batch changed authority beyond one good heartbeat")
    path = coordinator_private_root(state) / "fast-receipts.sqlite3"
    with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM intents").fetchall()
    if len(rows) != 16 or {row["receipt_id"] for row in rows} != {
        item["receipt_id"] for item in completed.values()
    }:
        raise RuntimeError("fast hostile public receipts disagree with durable queue")
    project_id = json.loads((state / "coordinator.binding.json").read_text())["project_id"]
    by_receipt = {row["receipt_id"]: row for row in rows}
    for index, command in enumerate(fast_hostile_commands()):
        public = completed[index]
        row = by_receipt[public["receipt_id"]]
        payload = {
            "expected_revision": int(command[6]),
            "lease_minutes": int(command[8]),
            "operation": "heartbeat",
            "owner": command[4],
            "project_id": project_id,
            "task": task,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        expected_phase = "completed-local" if index == 8 else "rejected"
        expected_oid = commits[0] if index == 8 else None
        if (
            row["project_id"] != project_id
            or row["idempotency_key"] != command[10]
            or row["operation"] != "heartbeat"
            or row["task_id"] != task
            or row["expected_revision"] != int(command[6])
            or row["payload_json"] != canonical
            or row["input_digest"] != hashlib.sha256(canonical.encode()).hexdigest()
            or row["phase"] != expected_phase
            or row["commit_oid"] != expected_oid
            or row["result_revision"] != (3 if index == 8 else None)
            or row["error_code"] != (None if index == 8 else "ADMISSION_REJECTED")
            or row["remote_oid"] is not None
            or any(
                public[field] != row[field]
                for field in (
                    "receipt_id",
                    "project_id",
                    "operation",
                    "task_id",
                    "expected_revision",
                    "phase",
                    "started_at",
                    "commit_oid",
                    "result_revision",
                    "error_code",
                    "remote_oid",
                    "remote_observed_at",
                    "publication_error",
                    "created_at",
                )
            )
        ):
            raise RuntimeError(
                "fast hostile durable row differs from typed request or public receipt"
            )
    if (
        dirty_checkout_digest(state) != before_dirty
        or runtime_digest(state, normalize_time=False) != before_runtime
        or non_head_refs_digest(state) != before_refs
        or coordinator_other_digest(state) != before_private
    ):
        raise RuntimeError("fast hostile batch changed unrelated state")
    checks = doctor(state, env)
    if any(checks.values()):
        raise RuntimeError("fast hostile post-batch doctors failed")
    print(
        json.dumps(
            {
                "batch": "fast_hostile",
                "workers": 16,
                "malicious_rejected": 15,
                "good_completed_local": 1,
                "signed_commits": 1,
                "good_local_wall_ms": round(good_local_wall_ms, 1),
                "integrity": checks,
            }
        ),
        flush=True,
    )


def configure_fixture_signature_verification(state: Path) -> None:
    """Trust only the ephemeral key that signed this disposable state clone."""
    public_key = (state.parent / "signing-key.pub").read_text().strip()
    allowed = state.parent / "allowed-signers"
    allowed.write_text(f"fixture@example.invalid {public_key}\n")
    subprocess.run(  # noqa: S603
        ["git", "config", "gpg.ssh.allowedSignersFile", str(allowed)],  # noqa: S607
        cwd=state,
        check=True,
    )


def has_matching_dco_trailer(message: str, author_name: str, author_email: str) -> bool:
    trailers = subprocess.run(
        ["git", "interpret-trailers", "--parse"],  # noqa: S607
        input=message,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return f"Signed-off-by: {author_name} <{author_email}>" in trailers


def strict_acceptance_commit_errors(state: Path, starting_head: str) -> tuple[int, list[str]]:
    """Check ancestry, changed-path scope, SSH signatures, and DCO for strict commits."""
    revisions = subprocess.run(  # noqa: S603
        ["git", "rev-list", f"{starting_head}..HEAD"],  # noqa: S607
        cwd=state,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    errors: list[str] = []
    if len(revisions) != len(TASK_IDS) or not history_extends(state, starting_head):
        errors.append("acceptance batch did not add 16 extending commits")
    seen_tasks: set[str] = set()
    for commit_hash in revisions:
        parents = subprocess.run(  # noqa: S603
            ["git", "show", "-s", "--format=%P", commit_hash],  # noqa: S607
            cwd=state,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        if len(parents) != 1:
            errors.append(f"{commit_hash}: acceptance commit is not single-parent")
        changed_paths = subprocess.run(  # noqa: S603
            ["git", "diff-tree", "-r", "--no-commit-id", "--name-only", "-z", commit_hash],  # noqa: S607
            cwd=state,
            capture_output=True,
            check=True,
        ).stdout.split(b"\0")
        verified = subprocess.run(  # noqa: S603
            ["git", "verify-commit", commit_hash],  # noqa: S607
            cwd=state,
            capture_output=True,
            check=False,
        )
        if verified.returncode:
            errors.append(f"{commit_hash}: signature did not verify")
        identity_message = subprocess.run(  # noqa: S603
            ["git", "show", "-s", "--format=%an%x00%ae%x00%s%x00%B", commit_hash],  # noqa: S607
            cwd=state,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        author_name, author_email, subject, message = identity_message.split("\0", 3)
        prefix = "chore(state): accept "
        task_id = subject.removeprefix(prefix)
        if not subject.startswith(prefix) or task_id not in TASK_IDS or task_id in seen_tasks:
            errors.append(f"{commit_hash}: acceptance commit subject or task is invalid")
        else:
            seen_tasks.add(task_id)
            if [path for path in changed_paths if path] != [f"tasks/{task_id}.md".encode()]:
                errors.append(f"{commit_hash}: unrelated committed path changed")
        if (author_name, author_email) != ("Fixture", "fixture@example.invalid") or not (
            has_matching_dco_trailer(message, author_name, author_email)
        ):
            errors.append(f"{commit_hash}: matching DCO trailer missing")
    if seen_tasks != set(TASK_IDS):
        errors.append("acceptance batch did not commit each task exactly once")
    return len(revisions), errors


def run_acceptance_probe(
    state: Path,
    env: dict[str, str],
    timeout: float,
    source_state: Path,
    source_product: Path,
    state_head: str,
    state_digest: str,
    product_digest: str,
) -> None:
    configure_fixture_signature_verification(state)
    before = {task_id: task_meta(state, task_id) for task_id in TASK_IDS}
    before_bodies = {
        task_id: (state / "tasks" / f"{task_id}.md").read_text().split("---", 2)[2]
        for task_id in TASK_IDS
    }
    starting_head = git_head(state)
    before_dirty = dirty_checkout_digest(state)
    before_runtime = runtime_digest(state, normalize_time=False)
    before_refs = non_head_refs_digest(state)
    checked_batch(state, env, "acceptances", acceptances(state), timeout, 16)
    errors = acceptance_errors(state, before)
    for task_id in TASK_IDS:
        after = task_meta(state, task_id)
        body = (state / "tasks" / f"{task_id}.md").read_text().split("---", 2)[2]
        expected = (
            before_bodies[task_id]
            + f"\n- {after['updated_at']}: Disposable concurrency acceptance.\n"
        )
        if body != expected:
            errors.append(f"{task_id}: unrelated task body changed")
    commit_count, commit_errors = strict_acceptance_commit_errors(state, starting_head)
    errors.extend(commit_errors)
    if dirty_checkout_digest(state) != before_dirty:
        errors.append("acceptance batch left uncommitted state changes")
    if runtime_digest(state, normalize_time=False) != before_runtime:
        errors.append("acceptance batch changed ignored runtime state")
    if non_head_refs_digest(state) != before_refs:
        errors.append("acceptance batch changed non-HEAD Git refs")
    if errors:
        raise RuntimeError("acceptance durability mismatch: " + "; ".join(errors))
    print(
        json.dumps(
            {
                "after": "acceptances",
                "matching_acceptance_records": len(TASK_IDS),
                "verified_signed_dco_commits": commit_count,
                "no_unrelated_effects": True,
            }
        ),
        flush=True,
    )
    require_unchanged_sources(
        {
            "source_product_inputs_changed": product_input_digest(source_product) != product_digest,
            "source_state_head_changed": git_head(source_state) != state_head,
            "source_state_inputs_changed": product_input_digest(source_state, include_runtime=True)
            != state_digest,
        }
    )


def main() -> None:  # noqa: C901
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--product", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--only-mutations", action="store_true")
    parser.add_argument("--only-adversarial", action="store_true")
    parser.add_argument("--only-accept", action="store_true")
    parser.add_argument("--only-roles", action="store_true")
    parser.add_argument("--only-fast-heartbeat", action="store_true")
    parser.add_argument("--only-fast-hostile", action="store_true")
    parser.add_argument("--only-strict-heartbeat", action="store_true")
    args = parser.parse_args()
    source_state = args.state.resolve()
    source_product = args.product.resolve()
    state_head = git_head(source_state)
    state_digest = product_input_digest(source_state, include_runtime=True)
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
        stabilized_claims = stabilize_disposable_claims(state)
        configure(state, source_product, str(binding["product_repository"]), env)
        add_fixture_tasks(state, env)
        print(
            json.dumps(
                {
                    "source_state": state_head,
                    "candidate": git_head(runtime),
                    "fixture_tasks": len(TASK_IDS),
                    "stabilized_claims": stabilized_claims,
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
            if args.only_mutations
            or args.only_adversarial
            or args.only_accept
            or args.only_fast_heartbeat
            or args.only_fast_hostile
            or args.only_strict_heartbeat
            else initial_batches
        )
        for name, commands in batches:
            checked_batch(state, env, name, commands, args.timeout_seconds, 16)
        if args.only_strict_heartbeat:
            configure_fixture_signature_verification(state)
            if list(coordinator_private_root(state).glob("fast-receipts.sqlite3*")):
                raise RuntimeError("strict heartbeat fixture already has a fast receipt queue")
            before = {task_id: task_meta(state, task_id) for task_id in TASK_IDS}
            before_bodies = {
                task_id: (state / "tasks" / f"{task_id}.md").read_text().split("---", 2)[2]
                for task_id in TASK_IDS
            }
            starting_head = git_head(state)
            before_dirty = dirty_checkout_digest(state)
            before_runtime = runtime_digest(state, normalize_time=False)
            before_refs = non_head_refs_digest(state)
            before_private = coordinator_other_digest(state)
            started_at = dt.datetime.now(dt.UTC)
            checked_batch(
                state,
                env,
                "strict_heartbeat",
                [
                    [
                        "heartbeat",
                        task_id,
                        "--owner",
                        f"bench-{index}",
                        "--expected-revision",
                        "2",
                        "--lease-minutes",
                        "20",
                    ]
                    for index, task_id in enumerate(TASK_IDS)
                ],
                args.timeout_seconds,
                16,
            )
            finished_at = dt.datetime.now(dt.UTC)
            count, errors = heartbeat_commit_errors(state, starting_head)
            errors.extend(
                heartbeat_effect_errors(state, before, before_bodies, started_at, finished_at)
            )
            if dirty_checkout_digest(state) != before_dirty:
                errors.append("strict heartbeat left uncommitted state")
            if runtime_digest(state, normalize_time=False) != before_runtime:
                errors.append("strict heartbeat changed ignored runtime state")
            if non_head_refs_digest(state) != before_refs:
                errors.append("strict heartbeat changed non-HEAD Git refs")
            if coordinator_other_digest(state) != before_private:
                errors.append("strict heartbeat changed unrelated private Git state")
            if list(coordinator_private_root(state).glob("fast-receipts.sqlite3*")):
                errors.append("strict heartbeat created a fast receipt queue")
            if errors:
                raise RuntimeError("strict heartbeat durability mismatch: " + "; ".join(errors))
            print(
                json.dumps(
                    {
                        "after": "strict_heartbeat",
                        "signed_commits": count,
                        "no_unrelated_effects": True,
                    }
                ),
                flush=True,
            )
            require_unchanged_sources(
                {
                    "source_product_inputs_changed": product_input_digest(source_product)
                    != product_digest,
                    "source_state_head_changed": git_head(source_state) != state_head,
                    "source_state_inputs_changed": product_input_digest(
                        source_state, include_runtime=True
                    )
                    != state_digest,
                }
            )
            return
        if args.only_fast_heartbeat:
            run_fast_heartbeat_probe(state, env, args.timeout_seconds)
            require_unchanged_sources(
                {
                    "source_product_inputs_changed": product_input_digest(source_product)
                    != product_digest,
                    "source_state_head_changed": git_head(source_state) != state_head,
                    "source_state_inputs_changed": product_input_digest(
                        source_state, include_runtime=True
                    )
                    != state_digest,
                }
            )
            return
        if args.only_fast_hostile:
            run_fast_hostile_probe(state, env, args.timeout_seconds)
            require_unchanged_sources(
                {
                    "source_product_inputs_changed": product_input_digest(source_product)
                    != product_digest,
                    "source_state_head_changed": git_head(source_state) != state_head,
                    "source_state_inputs_changed": product_input_digest(
                        source_state, include_runtime=True
                    )
                    != state_digest,
                }
            )
            return
        if args.only_accept:
            run_acceptance_probe(
                state,
                env,
                args.timeout_seconds,
                source_state,
                source_product,
                state_head,
                state_digest,
                product_digest,
            )
            return
        if not args.only_roles:
            adversarial = adversarial_probe(state, env, args.timeout_seconds)
            print(json.dumps(adversarial), flush=True)
            if not (
                adversarial["good_worker_live"]
                and adversarial["exactly_one_commit"]
                and adversarial["task_revision_advanced_once"]
                and adversarial["owner_preserved"]
                and adversarial["good_update_recorded_once"]
                and adversarial["unauthorized_subprocess_suppressed"]
                and adversarial["only_expected_durable_paths_changed"]
                and adversarial["no_uncommitted_effects"]
                and adversarial["runtime_unchanged"]
                and adversarial["non_head_refs_unchanged"]
                and adversarial["one_expected_session_record"]
                and adversarial["starting_history_preserved"]
            ):
                raise RuntimeError("adversarial concurrency integrity or liveness failure")
        if args.only_adversarial:
            require_unchanged_sources(
                {
                    "source_product_inputs_changed": product_input_digest(source_product)
                    != product_digest,
                    "source_state_head_changed": git_head(source_state) != state_head,
                    "source_state_inputs_changed": product_input_digest(
                        source_state, include_runtime=True
                    )
                    != state_digest,
                }
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
            checked_batch(
                state,
                env,
                name,
                commands,
                args.timeout_seconds,
                4 if name == "rejected_and_readers" else 16,
            )
        seed_active_roles(state, env)
        seed_checks = doctor(state, env)
        print(json.dumps({"after": "role_seed", "integrity": seed_checks}), flush=True)
        if any(seed_checks.values()):
            raise RuntimeError("role_seed: failed integrity check")
        checked_batch(
            state,
            env,
            "role_assignments",
            role_assignments(state),
            args.timeout_seconds,
            1,
        )
        for name, commands in (
            ("role_reads", role_reads(state)),
            ("role_removals", role_removals(state)),
        ):
            checked_batch(
                state,
                env,
                name,
                commands,
                args.timeout_seconds,
                1 if name == "role_removals" else 16,
            )
        require_unchanged_sources(
            {
                "source_product_inputs_changed": product_input_digest(source_product)
                != product_digest,
                "source_state_head_changed": git_head(source_state) != state_head,
                "source_state_inputs_changed": product_input_digest(
                    source_state, include_runtime=True
                )
                != state_digest,
            }
        )


if __name__ == "__main__":
    main()
