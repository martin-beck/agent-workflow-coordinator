# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Compare real Git-backed CLI read/reconcile latencies on disposable ASB copies.

Pass an exact baseline commit; never benchmark an implicit moving baseline.
Source repositories are observed only, and all coordinator writes stay in
temporary state clones. Results contain bounded aggregates, not raw state.
"""

import argparse
import concurrent.futures
import contextlib
import hashlib
import json
import math
import os
import re
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, BinaryIO

from git_scale_probe import prepare

from tools.vendor import SOURCE_FILES

SOURCE = Path(__file__).resolve().parents[1]
OVERLAY_PATHS = frozenset(os.fsencode(destination) for _source, destination in SOURCE_FILES)
ROUTES = {
    "doctor": ("doctor",),
    "doctor-live": ("doctor", "--live"),
    "snapshot": ("snapshot",),
    "render-check": ("render-status", "--check"),
    "reconcile": ("reconcile",),
}


def git_head(root: Path) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "rev-parse", "HEAD"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def history_depth(root: Path) -> int:
    return int(
        subprocess.run(  # noqa: S603
            ["/usr/bin/git", "-C", str(root), "rev-list", "--count", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )


def history_extends(root: Path, old_head: str) -> bool:
    result = subprocess.run(  # noqa: S603
        ["/usr/bin/git", "-C", str(root), "merge-base", "--is-ancestor", old_head, "HEAD"],
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError("unable to verify benchmark Git ancestry")
    return result.returncode == 0


def runtime_digest(root: Path, *, normalize_time: bool = True) -> bytes:
    """Fingerprint ignored runtime authority, optionally normalizing observation time."""
    runtime = root / ".runtime"
    digest = hashlib.sha256()
    if not runtime.exists():
        return digest.digest()
    for path in sorted(runtime.rglob("*")):
        if not path.is_file() or path.name.endswith(".lock"):
            continue
        relative = path.relative_to(runtime)
        data = path.read_bytes()
        if normalize_time and relative == Path("last-reconcile.json"):
            try:
                record = json.loads(data)
                if isinstance(record, dict) and "at" in record:
                    record["at"] = "<observed-time>"
                    data = json.dumps(record, sort_keys=True).encode()
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        digest.update(os.fsencode(str(relative)) + b"\0" + data + b"\0")
    return digest.digest()


def overlay_digest(root: Path) -> str:
    """Catch runtime-overlay mutation within one measured command."""
    entries = subprocess.run(  # noqa: S603
        ["/usr/bin/git", "-C", str(root), "ls-files", "--stage", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    digest = hashlib.sha256()
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        path = entry.split(b"\t", 1)[1]
        if path not in OVERLAY_PATHS:
            continue
        target = root / os.fsdecode(path)
        digest.update(entry + b"\0")
        if target.is_symlink():
            digest.update(b"symlink\0" + os.fsencode(target.readlink()))
        else:
            digest.update(target.read_bytes() if target.is_file() else b"missing\0")
    return digest.hexdigest()


def non_head_refs_digest(root: Path) -> str:
    """Detect branch/tag/remote ref effects other than the expected HEAD move."""
    branch = subprocess.run(  # noqa: S603
        ["/usr/bin/git", "-C", str(root), "symbolic-ref", "-q", "HEAD"],
        check=False,
        capture_output=True,
    )
    if branch.returncode not in {0, 1}:
        raise RuntimeError("unable to identify the benchmark HEAD ref")
    current = branch.stdout.strip() if branch.returncode == 0 else None
    refs = subprocess.run(  # noqa: S603
        ["/usr/bin/git", "-C", str(root), "show-ref"],
        check=True,
        capture_output=True,
    ).stdout
    digest = hashlib.sha256()
    for line in refs.splitlines():
        if line.rsplit(b" ", 1)[-1] != current:
            digest.update(line + b"\n")
    return digest.hexdigest()


def domain_tree(root: Path) -> str:
    """Compare committed, staged, dirty, and untracked domain state bytes."""
    digest = hashlib.sha256()
    for flags in (("--stage",), ("--others", "--exclude-standard")):
        entries = subprocess.run(  # noqa: S603
            ["/usr/bin/git", "-C", str(root), "ls-files", *flags, "-z"],
            check=True,
            capture_output=True,
        ).stdout
        for entry in entries.split(b"\0"):
            if not entry:
                continue
            path = entry.split(b"\t", 1)[1] if flags == ("--stage",) else entry
            if path in OVERLAY_PATHS:
                continue
            checkout = root / os.fsdecode(path)
            digest.update(entry + b"\0")
            if checkout.is_symlink():
                digest.update(b"symlink\0" + os.fsencode(checkout.readlink()))
            elif checkout.is_file():
                with checkout.open("rb") as stream:
                    while chunk := stream.read(65536):
                        digest.update(chunk)
            else:
                digest.update(b"missing\0")
    digest.update(runtime_digest(root))
    return digest.hexdigest()


def worktree_listing(root: Path) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "worktree", "list", "--porcelain"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def dirty_checkout_digest(path: Path) -> bytes:
    """Fingerprint dirty tracked/index/untracked bytes without counting HEAD."""
    observed_env = dict(os.environ)
    observed_env["GIT_OPTIONAL_LOCKS"] = "0"
    observed = subprocess.run(  # noqa: S603
        ["/usr/bin/git", "-C", str(path), "status", "--porcelain=v1", "-z"],
        env=observed_env,
        check=True,
        capture_output=True,
    ).stdout
    checkout = hashlib.sha256(observed)
    if observed:
        for args in (("diff", "--binary", "HEAD"), ("diff", "--cached", "--binary", "HEAD")):
            diff = subprocess.run(  # noqa: S603
                ["/usr/bin/git", "-C", str(path), *args],
                env=observed_env,
                check=True,
                capture_output=True,
            ).stdout
            checkout.update(diff)
        untracked = subprocess.run(  # noqa: S603
            ["/usr/bin/git", "-C", str(path), "ls-files", "--others", "--exclude-standard", "-z"],
            env=observed_env,
            check=True,
            capture_output=True,
        ).stdout
        for name in untracked.split(b"\0"):
            if not name:
                continue
            entry = path / os.fsdecode(name)
            checkout.update(name)
            checkout.update(
                os.fsencode(entry.readlink()) if entry.is_symlink() else entry.read_bytes()
            )
    return checkout.digest()


def product_input_digest(root: Path, *, include_runtime: bool = False) -> str:
    """Detect net changes to listed heads, refs, and dirty checkout bytes."""
    listing = worktree_listing(root)
    paths = [Path(line[9:]) for line in listing.splitlines() if line.startswith("worktree ")]
    digest = hashlib.sha256(listing.encode())
    refs = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "show-ref"],  # noqa: S607
        check=True,
        capture_output=True,
    ).stdout
    digest.update(refs)
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        for path, observed in zip(paths, pool.map(dirty_checkout_digest, paths), strict=True):
            digest.update(str(path).encode())
            digest.update(observed)
            if include_runtime:
                digest.update(runtime_digest(path, normalize_time=False))
    return digest.hexdigest()


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))
    return round(ordered[index], 3)


def case_order(repetition: int) -> tuple[str, str]:
    return ("baseline", "candidate") if repetition % 2 == 0 else ("candidate", "baseline")


def resources() -> tuple[float, float, int, int]:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime, usage.ru_stime, usage.ru_inblock, usage.ru_oublock


def classify(returncode: int, stderr: bytes) -> str:
    if returncode == 0:
        return "ok"
    if b"LOCK_TIMEOUT" in stderr:
        return "lock_timeout"
    if b"stale" in stderr.lower() or b"OBSERVATION_CHANGED" in stderr:
        return "stale_or_changed"
    return "other_error"


def digest_stream(stream: BinaryIO) -> tuple[str, int]:
    stream.seek(0)
    digest = hashlib.sha256()
    length = 0
    while chunk := stream.read(65536):
        digest.update(chunk)
        length += len(chunk)
    return digest.hexdigest(), length


def snapshot_body_digest(stream: BinaryIO, expected_commit: str) -> str:
    """Compare snapshot bodies while preserving raw hashes of fixture commit IDs."""
    stream.seek(0)
    first = stream.readline(128)
    if re.fullmatch(rb"STATE_COMMIT=[0-9a-f]{40}\n", first) is None or first != (
        f"STATE_COMMIT={expected_commit}\n".encode()
    ):
        raise ValueError("Git snapshot did not print its exact fixture commit")
    digest = hashlib.sha256()
    while chunk := stream.read(65536):
        digest.update(chunk)
    return digest.hexdigest()


def wait_for_processes(
    running: list[tuple[subprocess.Popen[bytes], float, BinaryIO, BinaryIO]],
    route: str,
    started: float,
    timeout: float,
    expected_commit: str,
) -> dict[int, tuple[float, str, str, int, str]]:
    completed: dict[int, tuple[float, str, str, int, str]] = {}
    while len(completed) != len(running):
        if time.monotonic() - started > timeout:
            for process, _, _, _ in running:
                if process.poll() is None:
                    process.kill()
            for process, _, _, _ in running:
                process.wait()
            raise TimeoutError(f"{route} exceeded the bounded {timeout:.0f}s batch deadline")
        for process, _, stdout, stderr in running:
            if process.pid not in completed and process.poll() is not None:
                process.wait()
                stderr.seek(0)
                diagnostic = stderr.read(1_000_000)
                output_hash, output_bytes = digest_stream(stdout)
                body_hash = (
                    snapshot_body_digest(stdout, expected_commit)
                    if route == "snapshot" and process.returncode == 0
                    else output_hash
                )
                completed[process.pid] = (
                    time.monotonic(),
                    classify(process.returncode, diagnostic),
                    output_hash,
                    output_bytes,
                    body_hash,
                )
        time.sleep(0.005)
    return completed


def measure(
    state: Path, env: dict[str, str], route: str, workers: int, timeout: float
) -> dict[str, object]:
    started = time.monotonic()
    expected_commit = git_head(state)
    overlay_before = overlay_digest(state)
    refs_before = non_head_refs_digest(state)
    before = resources()
    with contextlib.ExitStack() as files:
        running: list[tuple[subprocess.Popen[bytes], float, BinaryIO, BinaryIO]] = []
        for _ in range(workers):
            stdout = files.enter_context(tempfile.TemporaryFile())
            stderr = files.enter_context(tempfile.TemporaryFile())
            process = subprocess.Popen(  # noqa: S603
                [sys.executable, "tools/handoffctl.py", *ROUTES[route]],
                cwd=state,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
            )
            running.append((process, time.monotonic(), stdout, stderr))
        completed = wait_for_processes(running, route, started, timeout, expected_commit)
    elapsed = time.monotonic() - started
    after = resources()
    latencies = [(completed[process.pid][0] - launch) * 1000 for process, launch, _, _ in running]
    outcomes: dict[str, int] = {}
    for process, _, _, _ in running:
        result = completed[process.pid][1]
        outcomes[result] = outcomes.get(result, 0) + 1
    return {
        "route": route,
        "workers": workers,
        "wall_ms": round(elapsed * 1000, 3),
        "latency_ms": {
            "p50": percentile(latencies, 0.5),
            "p95": percentile(latencies, 0.95),
            "max": percentile(latencies, 1),
        },
        "outcomes": outcomes,
        "stdout_sha256": sorted(completed[process.pid][2] for process, _, _, _ in running),
        "snapshot_body_sha256": sorted(completed[process.pid][4] for process, _, _, _ in running)
        if route == "snapshot"
        else None,
        "stdout_bytes": [completed[process.pid][3] for process, _, _, _ in running],
        "state_tree": domain_tree(state),
        "overlay_unchanged": overlay_before == overlay_digest(state),
        "non_head_refs_unchanged": refs_before == non_head_refs_digest(state),
        "history_extends": history_extends(state, expected_commit),
        "history_depth": history_depth(state),
        "current_sha256": hashlib.sha256((state / "CURRENT.md").read_bytes()).hexdigest(),
        "child_cpu_ms": {
            "user": round((after[0] - before[0]) * 1000, 3),
            "system": round((after[1] - before[1]) * 1000, 3),
        },
        "child_blocks": {
            "input": after[2] - before[2],
            "output": after[3] - before[3],
        },
    }


def prepare_runtime(base: Path, revision: str, label: str) -> Path:
    runtime = base / f"{label}-runtime"
    subprocess.run(  # noqa: S603
        ["git", "clone", "--shared", "-q", str(SOURCE), str(runtime)],  # noqa: S607
        check=True,
    )
    subprocess.run(  # noqa: S603
        ["git", "-C", str(runtime), "checkout", "-q", "--detach", revision],  # noqa: S607
        check=True,
    )
    if git_head(runtime) != revision:
        raise RuntimeError(f"{label} runtime does not match the requested commit")
    return runtime


def configure(
    state: Path, observed_product: Path, product_repository: str, env: dict[str, str]
) -> None:
    key = state.parent / "signing-key"
    subprocess.run(  # noqa: S603
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],  # noqa: S607
        check=True,
        stdout=subprocess.DEVNULL,
    )
    for field, value in (
        ("user.name", "Fixture"),
        ("user.email", "fixture@example.invalid"),
        ("gpg.format", "ssh"),
        ("user.signingkey", str(key)),
    ):
        subprocess.run(  # noqa: S603
            ["git", "-C", str(state), "config", field, value],  # noqa: S607
            check=True,
            stdout=subprocess.DEVNULL,
        )
    (state / ".runtime/config.json").write_text(
        json.dumps(
            {
                "projects_root": str(observed_product.parent),
                "product_worktree": observed_product.name,
                "github_repository": product_repository,
                "push_enabled": False,
            }
        )
        + "\n"
    )
    result = subprocess.run(
        [sys.executable, "tools/handoffctl.py", "reconcile", "--commit"],
        cwd=state,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=180,
        check=False,
    )
    if result.returncode:
        diagnostic = result.stderr.decode(errors="replace")
        for path in (state, observed_product):
            diagnostic = diagnostic.replace(str(path), "<fixture>")
        raise RuntimeError("disposable state setup reconcile failed: " + diagnostic[:800])


def run_samples(
    instances: dict[str, tuple[Path, dict[str, str]]],
    routes: tuple[str, ...],
    counts: tuple[int, ...],
    repetitions: int,
    timeout: float,
) -> bool:
    equivalent = True
    for route in routes:
        for count in counts:
            for repetition in range(repetitions):
                paired: dict[str, dict[str, object]] = {}
                for label in case_order(repetition):
                    state, env = instances[label]
                    result = measure(state, env, route, count, timeout)
                    paired[label] = result
                    print(
                        json.dumps(
                            {"case": label, "repetition": repetition + 1, **result},
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                baseline = paired["baseline"]
                candidate = paired["candidate"]
                outputs_equal = (
                    baseline["snapshot_body_sha256"] == candidate["snapshot_body_sha256"]
                    if route == "snapshot"
                    else baseline["stdout_sha256"] == candidate["stdout_sha256"]
                )
                pair_ok = (
                    baseline["outcomes"] == candidate["outcomes"] == {"ok": count}
                    and outputs_equal
                    and baseline["state_tree"] == candidate["state_tree"]
                    and baseline["history_depth"] == candidate["history_depth"]
                    and baseline["history_extends"] is True
                    and candidate["history_extends"] is True
                    and baseline["overlay_unchanged"] is True
                    and candidate["overlay_unchanged"] is True
                    and baseline["non_head_refs_unchanged"] is True
                    and candidate["non_head_refs_unchanged"] is True
                    and baseline["current_sha256"] == candidate["current_sha256"]
                )
                equivalent = equivalent and pair_ok
                print(
                    json.dumps(
                        {
                            "route": route,
                            "workers": count,
                            "repetition": repetition + 1,
                            "outcomes_equal": baseline["outcomes"] == candidate["outcomes"],
                            "stdout_equal": baseline["stdout_sha256"] == candidate["stdout_sha256"],
                            "stdout_lengths_equal": baseline["stdout_bytes"]
                            == candidate["stdout_bytes"],
                            "snapshot_bodies_equal": baseline["snapshot_body_sha256"]
                            == candidate["snapshot_body_sha256"]
                            if route == "snapshot"
                            else None,
                            "state_trees_equal": baseline["state_tree"] == candidate["state_tree"],
                            "history_depths_equal": baseline["history_depth"]
                            == candidate["history_depth"],
                            "histories_extend": baseline["history_extends"] is True
                            and candidate["history_extends"] is True,
                            "overlays_unchanged": baseline["overlay_unchanged"] is True
                            and candidate["overlay_unchanged"] is True,
                            "non_head_refs_unchanged": baseline["non_head_refs_unchanged"] is True
                            and candidate["non_head_refs_unchanged"] is True,
                            "current_views_equal": baseline["current_sha256"]
                            == candidate["current_sha256"],
                            "pair_qualified": pair_ok,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
    return equivalent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--product", type=Path, required=True)
    parser.add_argument("--baseline", required=True, help="exact 40-character source commit")
    parser.add_argument("--counts", default="1,2")
    parser.add_argument("--routes", default=",".join(ROUTES))
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=180)
    args = parser.parse_args()
    baseline = str(args.baseline)
    if len(baseline) != 40 or any(char not in "0123456789abcdef" for char in baseline):
        raise SystemExit("baseline must be an exact 40-character lowercase commit")
    counts = tuple(int(value) for value in args.counts.split(","))
    if not counts or any(count not in {1, 2, 4, 8, 16, 32, 64} for count in counts):
        raise SystemExit("counts must be a subset of 1,2,4,8,16,32,64")
    routes = tuple(args.routes.split(","))
    if not routes or any(route not in ROUTES for route in routes):
        raise SystemExit("unknown or empty route selection")
    if not 1 <= args.repetitions <= 10:
        raise SystemExit("repetitions must be between 1 and 10")
    source_state = args.state.resolve()
    source_product = args.product.resolve()
    state_head = git_head(source_state)
    state_digest = product_input_digest(source_state, include_runtime=True)
    product_head = git_head(source_product)
    candidate_head = git_head(SOURCE)
    product_binding: dict[str, Any] = json.loads(
        (source_state / "coordinator.binding.json").read_text()
    )
    task_count = len(list((source_state / "tasks").glob("AR-*.md")))
    listing = worktree_listing(source_product)
    worktree_count = listing.count("worktree ")
    product_digest = product_input_digest(source_product)
    with tempfile.TemporaryDirectory(prefix="awc-git-command-latency-") as temporary:
        base = Path(temporary)
        baseline_runtime = prepare_runtime(base, baseline, "baseline")
        candidate_runtime = prepare_runtime(base, candidate_head, "candidate")
        print(
            json.dumps(
                {
                    "source_state": state_head,
                    "source_state_input_digest": state_digest,
                    "source_product": product_head,
                    "baseline": baseline,
                    "candidate": candidate_head,
                    "tasks": task_count,
                    "product_worktrees": worktree_count,
                    "source_product_read_only": True,
                    "source_product_input_digest": product_digest,
                    "replication_enabled": False,
                    "github_observation": "fixed_empty_local_stub",
                },
                sort_keys=True,
            ),
            flush=True,
        )
        # Reconcile the pinned product observations once before cloning either
        # runtime. Otherwise time-stamped observation refreshes make two valid
        # implementations produce different fixture commits at setup time.
        normalized_state, _normalized_product, normalized_env = prepare(
            base,
            source_state,
            source_product,
            "normalizer",
            True,
            runtime_source=candidate_runtime,
            state_commit=state_head,
        )
        configure(
            normalized_state,
            source_product,
            str(product_binding["product_repository"]),
            normalized_env,
        )
        normalized_head = git_head(normalized_state)
        print(json.dumps({"normalized_fixture_state": normalized_head}), flush=True)
        instances: dict[str, tuple[Path, dict[str, str]]] = {}
        for label, runtime in (("baseline", baseline_runtime), ("candidate", candidate_runtime)):
            state, _product, env = prepare(
                base,
                normalized_state,
                source_product,
                label,
                True,
                runtime_source=runtime,
                state_commit=normalized_head,
            )
            configure(state, source_product, str(product_binding["product_repository"]), env)
            instances[label] = state, env
        fixture_trees = {label: domain_tree(state) for label, (state, _) in instances.items()}
        fixture_depths = {label: history_depth(state) for label, (state, _) in instances.items()}
        fixture_equal = (
            len(set(fixture_trees.values())) == 1 and len(set(fixture_depths.values())) == 1
        )
        print(json.dumps({"fixture_state_trees_equal": fixture_equal}))
        paired_equal = run_samples(
            instances, routes, counts, args.repetitions, args.timeout_seconds
        )
        source_state_changed = git_head(source_state) != state_head
        source_state_inputs_changed = (
            product_input_digest(source_state, include_runtime=True) != state_digest
        )
        source_product_changed = product_input_digest(source_product) != product_digest
        print(
            json.dumps(
                {
                    "source_state_head_changed": source_state_changed,
                    "source_state_inputs_changed": source_state_inputs_changed,
                    "source_product_head_changed": git_head(source_product) != product_head,
                    "source_worktree_listing_changed": worktree_listing(source_product) != listing,
                    "source_product_inputs_changed": source_product_changed,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        if (
            not fixture_equal
            or not paired_equal
            or source_state_changed
            or source_state_inputs_changed
            or source_product_changed
        ):
            raise RuntimeError("benchmark qualification invalid: fixture drift or unequal outcomes")


if __name__ == "__main__":
    main()
