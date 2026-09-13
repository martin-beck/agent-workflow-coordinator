# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Run one TLC model under durable admission and explicit resource bounds."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Sequence

DEFAULT_WORKERS = 2
DEFAULT_HEAP = "2048m"
DEFAULT_MEMORY_MAX = "3G"
DEFAULT_SWAP_MAX = "3G"


class AdmissionError(RuntimeError):
    """Raised when a TLC job cannot be admitted safely."""


def _positive_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise AdmissionError(f"{name} must be an integer") from error
    if parsed < 1:
        raise AdmissionError(f"{name} must be positive")
    return parsed


def _heap_bytes(heap: str) -> int:
    units = {"m": 1024**2, "g": 1024**3}
    suffix = heap[-1].lower()
    if suffix not in units:
        raise AdmissionError("heap must use an m or g suffix")
    return _positive_int(heap[:-1], "heap") * units[suffix]


def _prune_stale(queue: Path, max_age: int = 86400) -> None:
    now = time.time()
    for item in queue.glob("*.json"):
        try:
            if now - item.stat().st_mtime > max_age:
                item.unlink()
        except FileNotFoundError:
            continue


def build_command(
    *,
    jar: Path,
    model: Path,
    config: Path,
    metadir: Path,
    workers: int = DEFAULT_WORKERS,
    heap: str = DEFAULT_HEAP,
    memory_max: str = DEFAULT_MEMORY_MAX,
    swap_max: str = DEFAULT_SWAP_MAX,
    cpu_quota: str = "200%",
    tasks_max: int = 64,
    cgroup_mode: str = "required",
) -> list[str]:
    """Build a bounded TLC command without executing it."""
    worker_count = _positive_int(str(workers), "workers")
    process_limit = _positive_int(str(tasks_max), "tasks_max")
    heap_bytes = _heap_bytes(heap)
    if memory_max == "0" or swap_max == "0":
        raise AdmissionError("memory and swap limits must be non-zero")
    if heap_bytes >= _memory_bytes(memory_max):
        raise AdmissionError("JVM heap must be below the cgroup memory limit")
    java = [
        "java",
        f"-Xmx{heap}",
        "-XX:+UseParallelGC",
        f"-XX:ActiveProcessorCount={worker_count}",
        "-cp",
        str(jar),
        "tlc2.TLC",
        "-cleanup",
        "-config",
        str(config),
        "-metadir",
        str(metadir),
        "-workers",
        str(worker_count),
        str(model),
    ]
    if cgroup_mode == "off":
        return java
    systemd = shutil.which("systemd-run")
    if systemd is None:
        raise AdmissionError("systemd-run is required for TLC cgroup containment")
    return [
        systemd,
        "--user",
        "--quiet",
        "--wait",
        "--collect",
        "--pipe",
        "--service-type=exec",
        "--property=MemoryMax=" + memory_max,
        "--property=MemorySwapMax=" + swap_max,
        "--property=CPUQuota=" + cpu_quota,
        "--property=TasksMax=" + str(process_limit),
        "--",
        *java,
    ]


def _memory_bytes(value: str) -> int:
    suffix = value[-1].upper()
    units = {"M": 1024**2, "G": 1024**3}
    if suffix not in units:
        raise AdmissionError("memory limit must use an M or G suffix")
    return _positive_int(value[:-1], "memory limit") * units[suffix]


def run(args: argparse.Namespace) -> int:
    """Queue, admit, execute, and durably classify one TLC model."""
    queue = Path(args.queue).resolve()
    queue.mkdir(mode=0o700, parents=True, exist_ok=True)
    _prune_stale(queue)
    job = queue / f"{os.getpid()}-{uuid.uuid4().hex}.json"
    lock_path = queue / "admission.lock"
    record = {
        "model": str(args.model),
        "pid": os.getpid(),
        "state": "queued",
        "created": time.time(),
        "workers": args.workers,
        "heap": args.heap,
        "memory_max": args.memory_max,
        "swap_max": args.swap_max,
    }
    job.write_text(json.dumps(record, sort_keys=True) + "\n")
    try:
        command = build_command(
            jar=Path(args.jar),
            model=Path(args.model),
            config=Path(args.config),
            metadir=Path(args.metadir),
            workers=args.workers,
            heap=args.heap,
            memory_max=args.memory_max,
            swap_max=args.swap_max,
            cgroup_mode=args.cgroup_mode,
        )
        with lock_path.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            record["state"] = "running"
            job.write_text(json.dumps(record, sort_keys=True) + "\n")
            return subprocess.run(command, check=False).returncode
    except (AdmissionError, OSError) as error:
        print(f"TLC admission failed closed: {error}", file=sys.stderr)
        return 2
    finally:
        if job.exists():
            job.unlink()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--jar", required=True)
    result.add_argument("--model", required=True)
    result.add_argument("--config", required=True)
    result.add_argument("--metadir", required=True)
    result.add_argument(
        "--queue",
        default=os.environ.get("TLC_ADMISSION_QUEUE", "/tmp/agent-workflow-coordinator-tlc"),
    )
    result.add_argument(
        "--workers", type=int, default=int(os.environ.get("TLC_WORKERS", DEFAULT_WORKERS))
    )
    result.add_argument("--heap", default=os.environ.get("TLC_HEAP", DEFAULT_HEAP))
    result.add_argument(
        "--memory-max", default=os.environ.get("TLC_MEMORY_MAX", DEFAULT_MEMORY_MAX)
    )
    result.add_argument("--swap-max", default=os.environ.get("TLC_SWAP_MAX", DEFAULT_SWAP_MAX))
    result.add_argument("--cpu-quota", default=os.environ.get("TLC_CPU_QUOTA", "200%"))
    result.add_argument("--tasks-max", type=int, default=int(os.environ.get("TLC_TASKS_MAX", "64")))
    result.add_argument(
        "--cgroup-mode",
        choices=("required", "off"),
        default=os.environ.get("TLC_CGROUP_MODE", "required"),
    )
    return result


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
