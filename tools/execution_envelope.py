# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Strict, non-authorizing execution-envelope validation and preflight."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TASK = re.compile(r"^AR-[0-9]{4}$")
REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{2,255}$")
ENVELOPE_ID = re.compile(r"^AR-[0-9]{4}-e[1-9][0-9]*$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMAND_CLASSES = frozenset({"read", "test", "source-write", "git-publication"})


@dataclass(frozen=True)
class Preflight:
    """One deterministic, non-mutating command admission result."""

    allowed: bool
    code: str
    detail: str
    envelope_id: str = ""
    envelope_digest: str = ""

    def as_json(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "detail": self.detail,
            "envelope_id": self.envelope_id,
            "envelope_digest": self.envelope_digest,
        }


def digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _paths(value: object, field: str, errors: list[str]) -> None:
    if not isinstance(value, list) or not value or len(value) > 64:
        errors.append(f"{field} must be a bounded nonempty list")
        return
    if len(set(value)) != len(value):
        errors.append(f"{field} contains duplicates")
    for item in value:
        path = str(item)
        if not isinstance(item, str) or not REF.fullmatch(path) or path.startswith("."):
            errors.append(f"{field} contains an unsafe path")
            break


def _identity_errors(value: dict[str, Any]) -> list[str]:
    """Validate the stable envelope/task/spec identity fields."""
    result: list[str] = []
    if value.get("schema_version") != 1:
        result.append("envelope schema_version must be 1")
    envelope_id = value.get("envelope_id")
    task = value.get("task")
    if not isinstance(envelope_id, str) or not ENVELOPE_ID.fullmatch(envelope_id):
        result.append("envelope_id is invalid")
    if not isinstance(task, str) or not TASK.fullmatch(task):
        result.append("task is invalid")
    if (
        isinstance(envelope_id, str)
        and isinstance(task, str)
        and not envelope_id.startswith(task + "-")
    ):
        result.append("envelope_id does not match task")
    result.extend(
        f"{name} is invalid"
        for name in ("envelope_revision", "task_revision", "spec_revision")
        if type(value.get(name)) is not int or value[name] < 1
    )
    if not isinstance(value.get("spec_ref"), str) or not REF.fullmatch(value["spec_ref"]):
        result.append("spec_ref is invalid")
    return result


def _collection_errors(value: dict[str, Any]) -> list[str]:
    """Validate bounded descriptive collections without interpreting their text."""
    result: list[str] = []
    for name in ("outputs", "evidence", "stop_conditions"):
        items = value.get(name)
        if not isinstance(items, list) or not items or len(items) > 32:
            result.append(f"{name} must be a bounded nonempty list")
            continue
        ids = [item.get("id") for item in items if isinstance(item, dict)]
        if len(ids) != len(items) or len(set(ids)) != len(ids):
            result.append(f"{name} identifiers are invalid")
    non_goals = value.get("non_goals")
    valid_non_goals = (
        isinstance(non_goals, list)
        and bool(non_goals)
        and len(non_goals) <= 32
        and all(isinstance(item, str) and item.strip() and len(item) <= 240 for item in non_goals)
    )
    if not valid_non_goals:
        result.append("non_goals must be a bounded nonempty string list")
    return result


def _rule_errors(value: dict[str, Any]) -> list[str]:
    """Validate path, command, and stop-condition rules."""
    result: list[str] = []
    scope = value.get("path_scope")
    if not isinstance(scope, dict) or set(scope) != {"read", "write"}:
        result.append("path_scope must contain exactly read and write")
    else:
        _paths(scope["read"], "path_scope.read", result)
        _paths(scope["write"], "path_scope.write", result)
    classes = value.get("command_classes")
    if not isinstance(classes, dict) or not classes or set(classes) - COMMAND_CLASSES:
        result.append("command_classes are invalid")
    elif any(not _valid_command_rule(rule) for rule in classes.values()):
        result.append("command_classes contain an invalid rule")
    stops = value.get("stop_conditions")
    if isinstance(stops, list) and any(not _valid_stop_condition(item) for item in stops):
        result.append("stop_conditions contain an invalid rule")
    return result


def _valid_command_rule(rule: object) -> bool:
    return (
        isinstance(rule, dict)
        and set(rule) <= {"allowed", "timeout_seconds"}
        and type(rule.get("allowed")) is bool
        and (
            "timeout_seconds" not in rule
            or (type(rule["timeout_seconds"]) is int and 1 <= rule["timeout_seconds"] <= 3600)
        )
    )


def _valid_stop_condition(item: object) -> bool:
    return (
        isinstance(item, dict)
        and set(item) == {"id", "kind"}
        and item.get("kind") in {"deny", "pause"}
    )


def errors(value: object) -> list[str]:
    """Validate a closed version-one envelope without inferring authorization."""
    if not isinstance(value, dict):
        return ["envelope must be an object"]
    required = {
        "schema_version",
        "envelope_id",
        "envelope_revision",
        "task",
        "task_revision",
        "spec_ref",
        "spec_revision",
        "outputs",
        "non_goals",
        "path_scope",
        "command_classes",
        "evidence",
        "stop_conditions",
    }
    result = [f"envelope missing field: {name}" for name in sorted(required - set(value))]
    result.extend(f"envelope unknown field: {name}" for name in sorted(set(value) - required))
    result.extend(_identity_errors(value))
    result.extend(_collection_errors(value))
    result.extend(_rule_errors(value))
    return result


def load(root: Path, reference: str) -> tuple[dict[str, Any] | None, str, list[str]]:
    """Load one root-bound immutable envelope and return its exact digest."""
    if not REF.fullmatch(reference) or reference.startswith(".") or ".." in Path(reference).parts:
        return None, "", ["envelope reference is unsafe"]
    try:
        payload = (root / reference).read_bytes()
        value = json.loads(payload)
    except (OSError, json.JSONDecodeError) as error:
        return None, "", [f"cannot read envelope: {error}"]
    found = errors(value)
    return value if isinstance(value, dict) else None, digest(payload), found


def _bound_envelope(
    root: Path, meta: dict[str, Any]
) -> tuple[dict[str, Any] | None, Preflight | None]:
    """Load and verify the immutable envelope bound in task metadata."""
    reference = meta.get("execution_envelope_ref")
    if not isinstance(reference, str):
        return None, Preflight(False, "ENVELOPE_MISSING", "task has no bound execution envelope")
    envelope, actual_digest, found = load(root, reference)
    if found or envelope is None:
        return None, Preflight(False, "ENVELOPE_INVALID", "; ".join(found))
    if meta.get("execution_envelope_digest") != actual_digest:
        return None, Preflight(False, "ENVELOPE_DIGEST_MISMATCH", "bound envelope bytes changed")
    if (
        envelope["task"] != meta.get("id")
        or envelope["spec_ref"] != meta.get("spec_ref")
        or envelope["spec_revision"] != meta.get("spec_revision")
    ):
        return None, Preflight(
            False,
            "ENVELOPE_REVISION_MISMATCH",
            "envelope does not match durable task specification",
        )
    return envelope, None


def _scope_error(
    envelope: dict[str, Any], command_class: str, paths: list[str], worktree: Path
) -> Preflight | None:
    """Return a denial if declared paths leave their class-specific scope."""
    if command_class in {"source-write", "git-publication"} and not paths:
        return Preflight(
            False, "SCOPE_DECLARATION_REQUIRED", "mutating command classes require --scope-path"
        )
    allowed = envelope["path_scope"][
        "write" if command_class in {"source-write", "git-publication"} else "read"
    ]
    root = worktree.resolve()
    for declared in paths:
        candidate = (root / declared).resolve()
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError:
            return Preflight(
                False, "PATH_OUTSIDE_SCOPE", "declared path escapes the bound worktree"
            )
        if not any(
            relative == scope.rstrip("/") or relative.startswith(scope.rstrip("/") + "/")
            for scope in allowed
        ):
            return Preflight(
                False, "PATH_OUTSIDE_SCOPE", "declared path is outside the command-class scope"
            )
    return None


def preflight(
    root: Path,
    meta: dict[str, Any],
    owner: str,
    command_class: str,
    paths: list[str],
    worktree: Path,
) -> Preflight:
    """Evaluate only durable task/envelope facts; this function has no side effects."""
    envelope, denial = _bound_envelope(root, meta)
    if denial is not None or envelope is None:
        return denial or Preflight(False, "ENVELOPE_INVALID", "envelope could not be loaded")
    if meta.get("owner") != owner or meta.get("status") != "in_progress":
        return Preflight(False, "OWNER_CLAIM_REQUIRED", "active durable owner claim is required")
    if (
        command_class not in COMMAND_CLASSES
        or command_class not in envelope["command_classes"]
        or not envelope["command_classes"][command_class]["allowed"]
    ):
        return Preflight(
            False, "COMMAND_CLASS_DENIED", "command class is not allowed by the envelope"
        )
    scope_denial = _scope_error(envelope, command_class, paths, worktree)
    if scope_denial is not None:
        return scope_denial
    return Preflight(
        True,
        "ALLOWED",
        "durable envelope admission passed",
        envelope["envelope_id"],
        str(meta["execution_envelope_digest"]),
    )
