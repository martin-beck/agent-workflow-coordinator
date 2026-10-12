# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Deterministic, non-destructive Git worktree change-drift records."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

type Meta = dict[str, Any]

SCHEMA_VERSION = 1
MAX_RECORDS = 32
TASK = re.compile(r"^AR-[0-9]{4}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
KINDS = frozenset({"baseline", "delta", "explain", "rescope"})
CLASSIFICATIONS = frozenset({"in_scope", "out_of_scope"})


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def safe_path(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 1024
        and "\x00" not in value
        and not Path(value).is_absolute()
        and all(part not in {"", ".", ".."} for part in Path(value).parts)
    )


def normalize_scope(values: list[str]) -> list[str]:
    if not values or len(values) > 64 or not all(safe_path(value) for value in values):
        raise ValueError("change-drift scope must be a bounded list of safe paths")
    normalized = sorted(set(values))
    if len(normalized) != len(values):
        raise ValueError("change-drift scope contains duplicates")
    return normalized


def _inside(path: str, scope: list[str]) -> bool:
    return any(path == item or path.startswith(item + "/") for item in scope)


def _generated(path: str) -> bool:
    parts = Path(path).parts
    name = Path(path).name.lower()
    return any(
        part.lower() in {"build", "dist", "generated", "__generated__"} for part in parts
    ) or name.endswith((".generated", ".gen.py", ".pb.go", ".min.js"))


def _status_entries(raw: bytes) -> list[Meta]:
    """Parse porcelain v1 -z without interpreting hostile filenames as text lines."""
    fields = raw.split(b"\0")
    entries: list[Meta] = []
    index = 0
    while index < len(fields) - 1:
        field = fields[index]
        index += 1
        if len(field) < 4 or field[2:3] != b" ":
            raise ValueError("Git returned malformed porcelain status")
        status = field[:2].decode("ascii", "strict")
        path = field[3:].decode("utf-8", "surrogateescape")
        previous_path = ""
        if status[:1] in {"R", "C"} or status[1:2] in {"R", "C"}:
            if index >= len(fields) - 1:
                raise ValueError("Git returned truncated rename status")
            previous_path = fields[index].decode("utf-8", "surrogateescape")
            index += 1
        if not safe_path(path) or (previous_path and not safe_path(previous_path)):
            raise ValueError("Git returned an unsafe changed path")
        entries.append(
            {
                "path": path,
                "previous_path": previous_path,
                "status": status,
                "generated": _generated(path),
            }
        )
    return sorted(
        entries,
        key=lambda item: (str(item["path"]), str(item["previous_path"]), str(item["status"])),
    )


def observe(worktree: Path) -> Meta:
    """Take a stable, path-safe worktree observation without changing it."""
    root = worktree.resolve()
    head = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )
    status = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=all"],  # noqa: S607
        check=False,
        capture_output=True,
    )
    if status.returncode:
        raise RuntimeError("change-drift observation requires a Git worktree")
    return {
        "head": head.stdout.strip() if head.returncode == 0 else "",
        "entries": _status_entries(status.stdout),
    }


def _record(kind: str, task: str, revision: int, recorded_at: str, payload: Meta) -> Meta:
    if kind not in KINDS or not TASK.fullmatch(task) or revision < 1 or not recorded_at:
        raise ValueError("invalid change-drift record identity")
    identity = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "task": task,
        "task_revision": revision,
        "payload": payload,
    }
    return {**identity, "recorded_at": recorded_at, "digest": digest(identity)}


def baseline(task: str, revision: int, recorded_at: str, worktree: Path, scope: list[str]) -> Meta:
    return _record(
        "baseline",
        task,
        revision,
        recorded_at,
        {**observe(worktree), "scope": normalize_scope(scope)},
    )


def _entry_key(entry: Meta) -> tuple[str, str]:
    return str(entry["path"]), str(entry["previous_path"])


def delta(
    task: str,
    revision: int,
    recorded_at: str,
    worktree: Path,
    baseline_record: Meta,
    scope: list[str],
) -> Meta:
    current = observe(worktree)
    before = {_entry_key(entry): entry for entry in baseline_record["payload"]["entries"]}
    changed: list[Meta] = []
    for entry in current["entries"]:
        previous = before.get(_entry_key(entry))
        if previous == entry:
            continue
        paths = [str(entry["path"])] + (
            [str(entry["previous_path"])] if entry["previous_path"] else []
        )
        classification = (
            "in_scope" if all(_inside(path, scope) for path in paths) else "out_of_scope"
        )
        changed.append({**entry, "classification": classification, "material": True})
    payload = {"baseline_digest": baseline_record["digest"], **current, "entries": changed}
    return _record("delta", task, revision, recorded_at, payload)


def explain(
    task: str,
    revision: int,
    recorded_at: str,
    delta_digest: str,
    paths: list[str],
    reason: str,
    generated: bool,
) -> Meta:
    if (
        not DIGEST.fullmatch(delta_digest)
        or not paths
        or len(paths) > 64
        or not all(safe_path(path) for path in paths)
    ):
        raise ValueError("invalid change-drift explanation paths")
    if not reason.strip() or len(reason) > 1000 or "\n" in reason:
        raise ValueError("change-drift explanation reason is invalid")
    return _record(
        "explain",
        task,
        revision,
        recorded_at,
        {
            "delta_digest": delta_digest,
            "paths": sorted(set(paths)),
            "reason": reason,
            "generated": generated,
        },
    )


def rescope(
    task: str, revision: int, recorded_at: str, baseline_digest: str, scope: list[str]
) -> Meta:
    if not DIGEST.fullmatch(baseline_digest):
        raise ValueError("invalid change-drift baseline digest")
    return _record(
        "rescope",
        task,
        revision,
        recorded_at,
        {"baseline_digest": baseline_digest, "scope": normalize_scope(scope)},
    )


def validate(record: object) -> None:  # noqa: C901
    if not isinstance(record, dict) or set(record) != {
        "schema_version",
        "kind",
        "task",
        "task_revision",
        "recorded_at",
        "payload",
        "digest",
    }:
        raise ValueError("change-drift record fields are not exact")
    identity = {
        key: record[key] for key in ("schema_version", "kind", "task", "task_revision", "payload")
    }
    if (
        record.get("schema_version") != SCHEMA_VERSION
        or record.get("kind") not in KINDS
        or not TASK.fullmatch(str(record.get("task", "")))
        or type(record.get("task_revision")) is not int
        or int(record["task_revision"]) < 1
        or not isinstance(record.get("recorded_at"), str)
        or not record["recorded_at"]
        or record.get("digest") != digest(identity)
    ):
        raise ValueError("invalid change-drift record identity")
    payload = record["payload"]
    if not isinstance(payload, dict):
        raise ValueError("invalid change-drift record payload")
    kind = str(record["kind"])
    if kind == "baseline":
        _validate_observation(payload, with_scope=True)
    elif kind == "delta":
        if set(payload) != {"baseline_digest", "head", "entries"} or not DIGEST.fullmatch(
            str(payload.get("baseline_digest", ""))
        ):
            raise ValueError("invalid change-drift delta payload")
        _validate_observation(payload, with_scope=False, classified=True)
    elif kind == "explain":
        paths = payload.get("paths")
        if (
            set(payload) != {"delta_digest", "paths", "reason", "generated"}
            or not DIGEST.fullmatch(str(payload.get("delta_digest", "")))
            or not isinstance(paths, list)
            or not paths
            or len(paths) > 64
            or not all(safe_path(path) for path in paths)
            or len(set(paths)) != len(paths)
            or not isinstance(payload.get("reason"), str)
            or not str(payload["reason"]).strip()
            or len(str(payload["reason"])) > 1000
            or "\n" in str(payload["reason"])
            or type(payload.get("generated")) is not bool
        ):
            raise ValueError("invalid change-drift explanation payload")
    else:
        if set(payload) != {"baseline_digest", "scope"} or not DIGEST.fullmatch(
            str(payload.get("baseline_digest", ""))
        ):
            raise ValueError("invalid change-drift re-scope payload")
        try:
            if payload["scope"] != normalize_scope(payload["scope"]):
                raise ValueError("scope is not canonical")
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid change-drift re-scope payload") from error


def _validate_observation(  # noqa: C901
    payload: Meta, *, with_scope: bool, classified: bool = False
) -> None:
    expected = {"head", "entries"} | ({"scope"} if with_scope else {"baseline_digest"})
    if set(payload) != expected or not isinstance(payload.get("head"), str):
        raise ValueError("invalid change-drift observation payload")
    entries = payload.get("entries")
    if not isinstance(entries, list) or len(entries) > 4096:
        raise ValueError("invalid change-drift observation entries")
    expected_entry = {"path", "previous_path", "status", "generated"}
    if classified:
        expected_entry |= {"classification", "material"}
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != expected_entry
            or not safe_path(entry.get("path"))
            or (entry.get("previous_path") and not safe_path(entry.get("previous_path")))
            or not isinstance(entry.get("previous_path"), str)
            or not isinstance(entry.get("status"), str)
            or len(str(entry["status"])) != 2
            or type(entry.get("generated")) is not bool
            or (classified and entry.get("classification") not in CLASSIFICATIONS)
            or (classified and type(entry.get("material")) is not bool)
        ):
            raise ValueError("invalid change-drift observation entry")
    if entries != sorted(entries, key=lambda item: (*_entry_key(item), str(item["status"]))):
        raise ValueError("change-drift observation entries are not canonical")
    if with_scope:
        try:
            if payload["scope"] != normalize_scope(payload["scope"]):
                raise ValueError("scope is not canonical")
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid change-drift baseline payload") from error


def records(meta: Meta) -> list[Meta]:
    value = meta.get("change_drift", [])
    if not isinstance(value, list) or len(value) > MAX_RECORDS:
        raise ValueError("invalid change-drift history")
    for record in value:
        validate(record)
        if record["task"] != meta.get("id"):
            raise ValueError("change-drift record task differs from task")
    return value


def latest(records_value: list[Meta], kind: str) -> Meta | None:
    return next((record for record in reversed(records_value) if record["kind"] == kind), None)


def active_scope(records_value: list[Meta], baseline_record: Meta) -> list[str]:
    scope = list(baseline_record["payload"]["scope"])
    for record in records_value:
        if (
            record["kind"] == "rescope"
            and record["payload"]["baseline_digest"] == baseline_record["digest"]
        ):
            scope = list(record["payload"]["scope"])
    return scope


def unresolved(records_value: list[Meta]) -> list[Meta]:
    current = latest(records_value, "delta")
    if current is None:
        return []
    explained = {
        path
        for record in records_value
        if record["kind"] == "explain" and record["payload"]["delta_digest"] == current["digest"]
        for path in record["payload"]["paths"]
    }
    return [
        entry
        for entry in current["payload"]["entries"]
        if entry["material"] and entry["path"] not in explained
    ]
