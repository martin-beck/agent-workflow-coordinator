# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Internal Git fast-receipt executor; no queued intent is a completed task."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

if __package__:
    from .fast_receipts import ReceiptStore
else:  # pragma: no cover - direct vendored import
    from fast_receipts import ReceiptStore  # type: ignore[import-not-found,no-redef]

_OID = re.compile(r"[0-9a-f]{40}\Z")
_REJECTABLE = ("stale revision", "is owned by", "heartbeat requires an active task")


@contextmanager
def service_lock(core: Any) -> Iterator[None]:
    """Fence recovery and execution to one process per Git common directory."""
    path = core.coordinator_lock_path().parent / "fast-receipts.service.lock"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
    ):
        raise RuntimeError("fast receipt service directory is not private")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise RuntimeError("fast receipt service lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("fast receipt service is already active") from error
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _git(core: Any, *args: str, check: bool = True) -> str:
    command = ["git", "-C", str(core.ROOT), *args]
    result = core.run(command, check=check)
    return str(result.stdout)


def _task_meta_at_commit(core: Any, oid: str, task: str) -> dict[str, Any]:
    paths = _git(core, "diff-tree", "--no-commit-id", "--name-only", "-r", oid).splitlines()
    matches = [
        path
        for path in paths
        if path == f"tasks/{task}.md"
        or (path.startswith(f"tasks/{task}-") and path.endswith(".md"))
    ]
    if len(matches) != 1:
        raise RuntimeError("receipt commit does not change exactly one target task")
    source = _git(core, "show", f"{oid}:{matches[0]}")
    if not source.startswith("---\n"):
        raise RuntimeError("receipt task has no front matter")
    end = source.find("\n---\n", 4)
    if end < 0:
        raise RuntimeError("receipt task front matter is incomplete")
    value = json.loads(source[4:end])
    if not isinstance(value, dict):
        raise RuntimeError("receipt task front matter is invalid")
    return value


def _require_signed_branch_commit(core: Any, oid: str) -> None:
    if not _OID.fullmatch(oid):
        raise RuntimeError("invalid receipt commit oid")
    result = core.run(["git", "-C", str(core.ROOT), "verify-commit", oid], check=False)
    if result.returncode:
        raise RuntimeError("receipt commit signature is not trusted")
    ancestor = core.run(
        ["git", "-C", str(core.ROOT), "merge-base", "--is-ancestor", oid, "HEAD"],
        check=False,
    )
    if ancestor.returncode:
        raise RuntimeError("receipt commit is not on the active state branch")


def verify_local_commit(core: Any, intent: dict[str, Any], oid: str) -> int:
    """Verify exact marker, signed commit and task revision before local receipt."""
    _require_signed_branch_commit(core, oid)
    message = _git(core, "log", "-1", "--format=%B", oid)
    marker = f"Handoffctl-Receipt: {intent['receipt_id']}"
    lines = message.splitlines()
    if lines.count(marker) != 1:
        raise RuntimeError("receipt commit marker is missing or duplicated")
    if not lines or lines[0] != f"chore(state): heartbeat {intent['task_id']}":
        raise RuntimeError("receipt commit operation does not match intent")
    payload = json.loads(str(intent["payload_json"]))
    if not isinstance(payload, dict):
        raise RuntimeError("receipt intent payload is invalid")
    meta = _task_meta_at_commit(core, oid, str(intent["task_id"]))
    expected = int(intent["expected_revision"]) + 1
    if (
        meta.get("id") != intent["task_id"]
        or meta.get("owner") != payload.get("owner")
        or meta.get("status") != "in_progress"
        or meta.get("task_revision") != expected
    ):
        raise RuntimeError("receipt commit task state does not match intent")
    if core.project_settings()["commit_signoff"]:
        committer = _git(core, "show", "-s", "--format=%cn <%ce>", oid).strip()
        if lines.count(f"Signed-off-by: {committer}") != 1:
            raise RuntimeError("receipt commit DCO does not match committer")
    return expected


def find_receipt_commit(core: Any, receipt_id: str) -> str | None:
    marker = f"Handoffctl-Receipt: {receipt_id}"
    candidates = _git(core, "log", "--all", "--format=%H", "--grep", marker).splitlines()
    exact = [
        oid
        for oid in candidates
        if _OID.fullmatch(oid)
        and _git(core, "log", "-1", "--format=%B", oid).splitlines().count(marker) == 1
    ]
    if len(exact) > 1:
        raise RuntimeError("multiple commits carry one receipt marker")
    return exact[0] if exact else None


def _validated_heartbeat_intent(intent: dict[str, Any]) -> dict[str, Any]:
    payload_text = str(intent["payload_json"])
    payload = json.loads(payload_text)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if (
        not isinstance(payload, dict)
        or payload_text != canonical
        or hashlib.sha256(canonical.encode()).hexdigest() != intent["input_digest"]
        or payload.get("project_id") != intent["project_id"]
        or payload.get("operation") != "heartbeat"
        or payload.get("task") != intent["task_id"]
        or payload.get("expected_revision") != intent["expected_revision"]
        or not isinstance(payload.get("owner"), str)
        or not isinstance(payload.get("lease_minutes"), int)
    ):
        raise RuntimeError("receipt intent does not match canonical typed fields")
    return payload


def process_one(core: Any, store: ReceiptStore) -> dict[str, Any] | None:
    """Execute one intent; unknown post-commit state remains explicitly ambiguous."""
    core.assert_project_binding()
    if core.backend_selection()["backend"] != "git":
        raise RuntimeError("fast receipts require Git authority")
    intent = store.claim_next()
    if intent is None:
        return None
    receipt_id = str(intent["receipt_id"])
    try:
        payload = _validated_heartbeat_intent(intent)
        args = argparse.Namespace(
            task=intent["task_id"],
            owner=payload["owner"],
            lease_minutes=payload["lease_minutes"],
            expected_revision=intent["expected_revision"],
            _receipt_id=receipt_id,
        )
        core.mutate(args, "heartbeat")
        oid = str(args._committed_oid)
        revision = verify_local_commit(core, intent, oid)
        return store.record_local_commit(receipt_id, oid, revision)
    except Exception as error:
        try:
            recovered = find_receipt_commit(core, receipt_id)
            if recovered is not None:
                revision = verify_local_commit(core, intent, recovered)
                return store.record_local_commit(receipt_id, recovered, revision)
        except Exception:
            return store.record_ambiguity(receipt_id, "COMMIT_EVIDENCE_UNKNOWN")
        if any(text in str(error) for text in _REJECTABLE):
            return store.record_rejection(receipt_id, "ADMISSION_REJECTED")
        return store.record_ambiguity(receipt_id, "MUTATION_OUTCOME_UNKNOWN")


def recover_running(core: Any, store: ReceiptStore) -> list[dict[str, Any]]:
    """Resolve interrupted reservations without re-executing an external effect."""
    core.assert_project_binding()
    if core.backend_selection()["backend"] != "git":
        raise RuntimeError("fast receipts require Git authority")
    outcomes: list[dict[str, Any]] = []
    for intent in store.running():
        receipt_id = str(intent["receipt_id"])
        try:
            oid = find_receipt_commit(core, receipt_id)
            if oid is not None:
                revision = verify_local_commit(core, intent, oid)
                outcomes.append(store.record_local_commit(receipt_id, oid, revision))
                continue
        except Exception:
            outcomes.append(store.record_ambiguity(receipt_id, "COMMIT_EVIDENCE_UNKNOWN"))
            continue
        outcomes.append(store.record_ambiguity(receipt_id, "INTERRUPTED_BEFORE_COMMIT"))
    return outcomes


def process_pending(core: Any, store: ReceiptStore, *, limit: int = 16) -> list[dict[str, Any]]:
    """Recover first, then execute bounded queued work under one service lease."""
    if not 1 <= limit <= 1024:
        raise ValueError("invalid fast receipt batch limit")
    with service_lock(core):
        outcomes = recover_running(core, store)
        for _ in range(limit):
            result = process_one(core, store)
            if result is None:
                break
            outcomes.append(result)
        return outcomes


def _is_ancestor(core: Any, older: str, newer: str) -> bool:
    result = core.run(
        ["git", "-C", str(core.ROOT), "merge-base", "--is-ancestor", older, newer],
        check=False,
    )
    return bool(result.returncode == 0)


def _observe_remote_main(core: Any) -> str | None:
    observed = core.run(
        ["git", "-C", str(core.ROOT), "ls-remote", "--exit-code", "origin", "refs/heads/main"],
        check=False,
    )
    if observed.returncode:
        return None
    lines = str(observed.stdout).splitlines()
    if len(lines) != 1:
        return None
    fields = lines[0].split()
    if len(fields) != 2 or fields[1] != "refs/heads/main" or not _OID.fullmatch(fields[0]):
        return None
    oid = fields[0]
    present = core.run(
        ["git", "-C", str(core.ROOT), "cat-file", "-e", f"{oid}^{{commit}}"],
        check=False,
    )
    if present.returncode:
        core.run(
            ["git", "-C", str(core.ROOT), "fetch", "--no-tags", "origin", oid],
            check=False,
        )
        present = core.run(
            ["git", "-C", str(core.ROOT), "cat-file", "-e", f"{oid}^{{commit}}"],
            check=False,
        )
    return oid if present.returncode == 0 else None


def _publish_if_needed(
    core: Any, pending: list[dict[str, Any]], local_head: str, remote_head: str
) -> str | None:
    needs_push = any(
        not _is_ancestor(core, str(item["commit_oid"]), remote_head)
        for item in pending
        if _is_ancestor(core, str(item["commit_oid"]), local_head)
    )
    if not needs_push or not _is_ancestor(core, remote_head, local_head):
        return remote_head
    core.assert_project_binding()
    core.run(
        ["git", "-C", str(core.ROOT), "push", "origin", f"{local_head}:refs/heads/main"],
        check=False,
    )
    return _observe_remote_main(core)


def _finish_publications(
    core: Any,
    store: ReceiptStore,
    pending: list[dict[str, Any]],
    local_head: str,
    remote_head: str,
) -> list[dict[str, Any]]:
    outcomes = []
    for item in pending:
        receipt_id = str(item["receipt_id"])
        commit_oid = str(item["commit_oid"])
        if not _is_ancestor(core, commit_oid, local_head):
            outcomes.append(store.record_publication_failure(receipt_id, "LOCAL_BRANCH_MOVED"))
        elif _is_ancestor(core, commit_oid, remote_head):
            outcomes.append(store.record_remote_observation(receipt_id, remote_head))
        else:
            outcomes.append(
                store.record_publication_failure(receipt_id, "REMOTE_NOT_CONTAINING_COMMIT")
            )
    return outcomes


def publish_pending(core: Any, store: ReceiptStore) -> list[dict[str, Any]]:
    """Observe exact remote ancestry after optional non-force publication."""
    core.assert_project_binding()
    if core.backend_selection()["backend"] != "git":
        raise RuntimeError("fast receipts require Git authority")
    pending = store.pending_publication()
    if not pending:
        return []
    if not core.replication_enabled():
        return [
            store.record_publication_failure(str(item["receipt_id"]), "REPLICATION_DISABLED")
            for item in pending
        ]
    branch = _git(core, "symbolic-ref", "--short", "-q", "HEAD").strip()
    if branch != "main":
        return [
            store.record_publication_failure(str(item["receipt_id"]), "STATE_BRANCH_NOT_MAIN")
            for item in pending
        ]
    local_head = _git(core, "rev-parse", "HEAD").strip()
    if not _OID.fullmatch(local_head):
        raise RuntimeError("cannot identify local publication head")
    remote_head = _observe_remote_main(core)
    if remote_head is None:
        return [
            store.record_publication_failure(str(item["receipt_id"]), "REMOTE_UNKNOWN")
            for item in pending
        ]
    remote_head = _publish_if_needed(core, pending, local_head, remote_head)
    if remote_head is None:
        return [
            store.record_publication_failure(str(item["receipt_id"]), "REMOTE_UNKNOWN")
            for item in pending
        ]
    return _finish_publications(core, store, pending, local_head, remote_head)
