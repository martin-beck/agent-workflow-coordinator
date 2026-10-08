# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Probe Git contention on disposable copies of an existing project pair."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from tools.vendor import SOURCE_FILES  # noqa: E402


def checked(*argv: str, cwd: Path | None = None) -> None:
    subprocess.run(argv, cwd=cwd, check=True, stdout=subprocess.DEVNULL)  # noqa: S603


def prepare(
    base: Path, source_state: Path, source_product: Path, label: str, candidate: bool
) -> tuple[Path, Path, dict[str, str]]:
    root = base / label
    state = root / "state"
    product = root / "product"
    root.mkdir()
    checked("git", "clone", "-q", "--shared", str(source_state), str(state))
    checked("git", "clone", "-q", "--shared", str(source_product), str(product))
    state_origin = subprocess.run(  # noqa: S603
        ["git", "-C", str(source_state), "remote", "get-url", "origin"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    product_origin = subprocess.run(  # noqa: S603
        ["git", "-C", str(source_product), "remote", "get-url", "origin"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    checked("git", "-C", str(state), "remote", "set-url", "origin", state_origin)
    checked("git", "-C", str(product), "remote", "set-url", "origin", product_origin)
    if candidate:
        for source_name, destination_name in SOURCE_FILES:
            target = state / destination_name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE / source_name, target)
        # The released ASB vendor embeds these project-specific labels. Current
        # upstream requires the equivalent tracked, exact-HEAD extension.
        (state / "task-spec-policy.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "additional_evidence_classes": [
                        "hosted",
                        "journey",
                        "offline",
                        "privacy",
                        "quality",
                    ],
                },
                sort_keys=True,
            )
            + "\n"
        )
        checked("git", "-C", str(state), "add", "task-spec-policy.json")
        checked(
            "git",
            "-C",
            str(state),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "fixture task-spec policy",
        )
    (state / ".runtime").mkdir(exist_ok=True)
    bin_dir = root / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text("#!/usr/bin/env python3\nprint('[]')\n")
    gh.chmod(0o700)
    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    # Source worktrees are observed only; Git must not refresh their indexes.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return state, product, env


def probe(state: Path, env: dict[str, str], workers: int) -> dict[str, object]:  # noqa: C901
    running = []
    launched = time.monotonic()
    for index in range(workers):
        trace = state.parent / f"trace-{workers}-{index}.jsonl"
        worker_env = dict(env)
        worker_env["HANDOFFCTL_LOCK_TRACE"] = str(trace)
        process = subprocess.Popen(
            [sys.executable, "tools/handoffctl.py", "reconcile"],
            cwd=state,
            env=worker_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        running.append((process, trace, time.monotonic()))
    completed: dict[int, tuple[float, bytes]] = {}
    try:
        while len(completed) < len(running):
            if time.monotonic() - launched > 180:
                raise TimeoutError("scale probe exceeded 180 seconds")
            for process, _trace, _start in running:
                if process.pid not in completed and process.poll() is not None:
                    _, stderr = process.communicate()
                    completed[process.pid] = (time.monotonic(), stderr)
            time.sleep(0.01)
    except TimeoutError:
        for process, _trace, _start in running:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        raise
    latencies = []
    failures = []
    metrics: list[dict[str, Any]] = []
    for process, trace, start in running:
        ended, stderr = completed[process.pid]
        latencies.append(round(ended - start, 3))
        if process.returncode:
            failures.append("lock_timeout" if b"LOCK_TIMEOUT" in stderr else "other")
        if trace.exists():
            metrics.extend(json.loads(line) for line in trace.read_text().splitlines())
    return {
        "workers": workers,
        "wall_s": round(time.monotonic() - launched, 3),
        "latency_max_s": max(latencies),
        "lock_timeouts": failures.count("lock_timeout"),
        "other_failures": failures.count("other"),
        "max_lock_wait_ms": max((item["wait_ms"] for item in metrics), default=None),
        "max_lock_hold_ms": max(
            (item["hold_ms"] for item in metrics if item["hold_ms"] is not None),
            default=None,
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--product", type=Path, required=True)
    parser.add_argument("--diagnose-only", action="store_true")
    parser.add_argument("--candidate-only", action="store_true")
    parser.add_argument("--source-only", action="store_true")
    parser.add_argument("--copy-only", action="store_true")
    args = parser.parse_args()
    source_state = args.state.resolve()
    source_product = args.product.resolve()
    task_count = len(list((source_state / "tasks").glob("AR-*.md")))
    worktree_count = subprocess.run(  # noqa: S603
        ["git", "-C", str(source_product), "worktree", "list", "--porcelain"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.count("worktree ")
    print(json.dumps({"tasks": task_count, "source_product_worktrees": worktree_count}), flush=True)
    with tempfile.TemporaryDirectory(prefix="awc-git-scale-") as temporary:
        base = Path(temporary)
        if args.diagnose_only:
            state, product, env = prepare(base, source_state, source_product, "candidate", True)
            (state / ".runtime/config.json").write_text(
                json.dumps(
                    {
                        "projects_root": str(base),
                        "product_worktree": str(product),
                        "github_repository": "martin-beck/agent-systems-benchmark",
                        "push_enabled": False,
                    }
                )
            )
            diagnosis = subprocess.run(
                [sys.executable, "tools/handoffctl.py", "reconcile"],
                cwd=state,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            diagnostic = diagnosis.stderr
            for path in (state, product, source_state, source_product):
                diagnostic = diagnostic.replace(str(path), "<fixture>")
            print(json.dumps({"returncode": diagnosis.returncode, "diagnostic": diagnostic[:1200]}))
            return
        cases = (
            (("candidate", True),)
            if args.candidate_only
            else (("baseline", False), ("candidate", True))
        )
        for label, candidate in cases:
            state, product, env = prepare(base, source_state, source_product, label, candidate)
            products = (
                (("source_read_only", source_product),)
                if args.source_only
                else (("copy", product),)
                if args.copy_only
                else (("copy", product), ("source_read_only", source_product))
            )
            for product_label, observed in products:
                config_path = state / ".runtime/config.json"
                config_path.write_text(
                    json.dumps(
                        {
                            "projects_root": str(base),
                            "product_worktree": str(observed),
                            "github_repository": "martin-beck/agent-systems-benchmark",
                            "push_enabled": False,
                        }
                    )
                )
                counts = (
                    (1, 2, 4, 8, 16)
                    if candidate and product_label == "copy"
                    else ((1, 2, 4) if product_label == "copy" else (1, 2))
                )
                for workers in counts:
                    sample = probe(state, env, workers)
                    print(
                        json.dumps({"case": label, "product": product_label, **sample}), flush=True
                    )


if __name__ == "__main__":
    main()
