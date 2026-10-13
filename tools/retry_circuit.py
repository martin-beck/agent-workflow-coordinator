# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Bounded, revision-fenced retry circuit records for autonomous commands.

The circuit deliberately records only stable digests and concise operator supplied
reasons.  It never retains argv, stderr, paths, or other command output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from typing import Any

type Meta = dict[str, Any]

SCHEMA_VERSION = 1
DEFAULT_WINDOW = 3
MAX_WINDOW = 16
MAX_ATTEMPTS = 64
MAX_RECOVERIES = 32
TASK = re.compile(r"^AR-[0-9]{4}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMAND_DIGEST = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
FAILURES = frozenset({"EXIT", "SUBPROCESS_TIMEOUT", "RETRYABLE"})
RECOVERIES = frozenset({"checkpoint", "changed-approach", "retry-exception"})


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def normalized_identity(
    command_digest: str, command_class: str | None, scope_paths: list[str]
) -> str:
    """Return a stable privacy-safe identity for one retryable command shape."""
    if not COMMAND_DIGEST.fullmatch(command_digest):
        raise ValueError("retry circuit command digest is invalid")
    if command_class is not None and command_class not in {
        "read",
        "test",
        "source-write",
        "git-publication",
    }:
        raise ValueError("retry circuit command class is invalid")
    if len(scope_paths) > 64 or any(
        not isinstance(path, str)
        or not path
        or len(path) > 1024
        or "\x00" in path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in path.split("/"))
        for path in scope_paths
    ):
        raise ValueError("retry circuit scope is invalid")
    return digest(
        {
            "command_digest": command_digest,
            "command_class": command_class or "unbound",
            "scope": sorted(set(scope_paths)),
        }
    )


def progress_digest(meta: Meta) -> str:
    """Fingerprint durable progress without treating note/revision churn as progress."""
    drift = meta.get("change_drift")
    latest_drift = ""
    if isinstance(drift, list) and drift and isinstance(drift[-1], dict):
        latest_drift = str(drift[-1].get("digest", ""))
    return digest(
        {
            "checkpoint_commit": str(meta.get("checkpoint_commit", "")),
            "change_drift_digest": latest_drift,
            "spec_acceptance": str(
                meta.get("spec_acceptance", {}).get("evidence_digest", "")
                if isinstance(meta.get("spec_acceptance"), dict)
                else ""
            ),
        }
    )


def empty() -> Meta:
    return {"schema_version": SCHEMA_VERSION, "attempts": [], "recoveries": [], "hold": None}


def _reason(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500 or "\n" in value:
        raise ValueError("retry circuit recovery reason is invalid")
    return value.strip()


def _window(value: object) -> int:
    if type(value) is not int or not DEFAULT_WINDOW <= int(value) <= MAX_WINDOW:
        raise ValueError(f"retry circuit window must be {DEFAULT_WINDOW}..{MAX_WINDOW}")
    return int(value)


def _validate_attempt(attempt: object, previous_revision: int) -> int:
    if not isinstance(attempt, dict) or set(attempt) != {
        "identity",
        "failure",
        "progress_digest",
        "task_revision",
        "recorded_at",
        "window",
    }:
        raise ValueError("retry circuit attempt fields are invalid")
    if (
        not DIGEST.fullmatch(str(attempt.get("identity", "")))
        or attempt.get("failure") not in FAILURES
        or not DIGEST.fullmatch(str(attempt.get("progress_digest", "")))
        or type(attempt.get("task_revision")) is not int
        or int(attempt["task_revision"]) <= previous_revision
        or not isinstance(attempt.get("recorded_at"), str)
        or not attempt["recorded_at"]
    ):
        raise ValueError("retry circuit attempt is invalid")
    _window(attempt.get("window"))
    return int(attempt["task_revision"])


def _validate_recovery(recovery: object, previous_revision: int) -> int:
    if not isinstance(recovery, dict) or set(recovery) != {
        "identity",
        "kind",
        "reason",
        "task_revision",
        "recorded_at",
        "checkpoint_commit",
    }:
        raise ValueError("retry circuit recovery fields are invalid")
    if (
        not DIGEST.fullmatch(str(recovery.get("identity", "")))
        or recovery.get("kind") not in RECOVERIES
        or type(recovery.get("task_revision")) is not int
        or int(recovery["task_revision"]) <= previous_revision
        or not isinstance(recovery.get("recorded_at"), str)
        or not recovery["recorded_at"]
        or not isinstance(recovery.get("checkpoint_commit"), str)
        or (recovery["checkpoint_commit"] and not COMMIT.fullmatch(recovery["checkpoint_commit"]))
    ):
        raise ValueError("retry circuit recovery is invalid")
    _reason(recovery.get("reason"))
    return int(recovery["task_revision"])


def _validate_hold(hold: object, attempts: list[object]) -> None:
    if hold is None:
        return
    if not isinstance(hold, dict) or set(hold) != {
        "identity",
        "attempts",
        "progress_digest",
        "held_at",
        "window",
    }:
        raise ValueError("retry circuit hold fields are invalid")
    if (
        not DIGEST.fullmatch(str(hold.get("identity", "")))
        or type(hold.get("attempts")) is not int
        or int(hold["attempts"]) < DEFAULT_WINDOW
        or not DIGEST.fullmatch(str(hold.get("progress_digest", "")))
        or not isinstance(hold.get("held_at"), str)
        or not hold["held_at"]
    ):
        raise ValueError("retry circuit hold is invalid")
    if _window(hold.get("window")) != hold["attempts"]:
        raise ValueError("retry circuit hold threshold is invalid")
    matching = [
        item
        for item in attempts
        if isinstance(item, dict)
        and item["identity"] == hold["identity"]
        and item["progress_digest"] == hold["progress_digest"]
    ]
    if len(matching) < int(hold["attempts"]):
        raise ValueError("retry circuit hold has insufficient attempts")


def validate(value: object) -> Meta:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "attempts",
        "recoveries",
        "hold",
    }:
        raise ValueError("retry circuit fields are not exact")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("retry circuit schema is unsupported")
    attempts = value.get("attempts")
    recoveries = value.get("recoveries")
    hold = value.get("hold")
    if not isinstance(attempts, list) or not attempts or len(attempts) > MAX_ATTEMPTS:
        raise ValueError("retry circuit attempts are invalid")
    if not isinstance(recoveries, list) or len(recoveries) > MAX_RECOVERIES:
        raise ValueError("retry circuit recoveries are invalid")
    last_revision = 0
    for attempt in attempts:
        last_revision = _validate_attempt(attempt, last_revision)
    last_recovery_revision = 0
    for recovery in recoveries:
        last_recovery_revision = _validate_recovery(recovery, last_recovery_revision)
    _validate_hold(hold, attempts)
    return value


def metadata_error(meta: Meta) -> str | None:
    value = meta.get("retry_circuit")
    if value is None:
        return None
    try:
        validate(value)
    except ValueError as error:
        return str(error)
    return None


def _state(meta: Meta) -> Meta:
    value = meta.get("retry_circuit")
    return empty() if value is None else dict(validate(value))


def admit(meta: Meta, identity: str) -> None:
    """Reject an exact held attempt before the external command is launched."""
    state = _state(meta)
    hold = state["hold"]
    if hold is None:
        return
    current_progress = progress_digest(meta)
    if hold["identity"] == identity and hold["progress_digest"] == current_progress:
        raise RuntimeError(
            "RETRY_CIRCUIT_OPEN: repeated no-progress attempts require retry-circuit recovery"
        )


def record_failure(
    meta: Meta,
    identity: str,
    failure: str,
    window: int,
    recorded_at: str,
) -> str:
    """Append a failure and open the circuit exactly at the bounded threshold."""
    if failure not in FAILURES or not DIGEST.fullmatch(identity) or not recorded_at:
        raise ValueError("retry circuit failure input is invalid")
    threshold = _window(window)
    state = _state(meta)
    progress = progress_digest(meta)
    previous = [
        item
        for item in state["attempts"]
        if item["identity"] == identity and item["progress_digest"] == progress
    ]
    if previous and int(previous[-1]["window"]) != threshold:
        raise RuntimeError("retry circuit window changed within an active attempt series")
    state["attempts"] = [
        *state["attempts"],
        {
            "identity": identity,
            "failure": failure,
            "progress_digest": progress,
            "task_revision": int(meta["task_revision"]),
            "recorded_at": recorded_at,
            "window": threshold,
        },
    ][-MAX_ATTEMPTS:]
    count = len(previous) + 1
    if count >= threshold:
        state["hold"] = {
            "identity": identity,
            "attempts": threshold,
            "progress_digest": progress,
            "held_at": recorded_at,
            "window": threshold,
        }
        result = "Retry circuit opened after bounded unchanged failures."
    else:
        result = f"Recorded retryable failure {count}/{threshold}."
    validate(state)
    meta["retry_circuit"] = state
    return result


def recover(meta: Meta, identity: str, kind: str, reason: str, recorded_at: str) -> str:
    """Record a deliberate recovery and reopen only the held command identity."""
    if kind not in RECOVERIES or not DIGEST.fullmatch(identity) or not recorded_at:
        raise ValueError("retry circuit recovery input is invalid")
    state = _state(meta)
    hold = state["hold"]
    if hold is None or hold["identity"] != identity:
        raise RuntimeError("retry circuit recovery requires the exact held attempt identity")
    checkpoint = str(meta.get("checkpoint_commit", ""))
    if kind == "checkpoint" and not COMMIT.fullmatch(checkpoint):
        raise RuntimeError("retry circuit checkpoint recovery requires a recorded checkpoint")
    state["recoveries"] = [
        *state["recoveries"],
        {
            "identity": identity,
            "kind": kind,
            "reason": _reason(reason),
            "task_revision": int(meta["task_revision"]),
            "recorded_at": recorded_at,
            "checkpoint_commit": checkpoint,
        },
    ][-MAX_RECOVERIES:]
    state["hold"] = None
    validate(state)
    meta["retry_circuit"] = state
    return f"Retry circuit recovered by {kind}."


def add_command_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    item = commands.add_parser("retry-circuit")
    actions = item.add_subparsers(dest="retry_circuit_action", required=True)
    status = actions.add_parser("status")
    status.add_argument("task")
    for action in RECOVERIES:
        recovery = actions.add_parser(action)
        recovery.add_argument("task")
        recovery.add_argument("--owner", required=True)
        recovery.add_argument("--expected-revision", type=int, required=True)
        recovery.add_argument("--attempt", required=True)
        recovery.add_argument("--reason", required=True)


def apply(args: argparse.Namespace, meta: Meta, recorded_at: str) -> str:
    if meta.get("owner") != args.owner or meta.get("status") != "in_progress":
        raise RuntimeError("retry circuit recovery requires the active task owner")
    if args.expected_revision != meta.get("task_revision"):
        raise RuntimeError("retry circuit recovery requires the current task revision")
    return recover(
        meta, str(args.attempt), str(args.retry_circuit_action), str(args.reason), recorded_at
    )
