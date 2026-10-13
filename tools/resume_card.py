# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Deterministic, privacy-safe resume cards derived from durable task records."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

if __package__:
    from . import assumption_revalidation, change_drift, retry_circuit
else:  # pragma: no cover - direct handoffctl execution
    import assumption_revalidation  # type: ignore[import-not-found,no-redef]
    import change_drift  # type: ignore[import-not-found,no-redef]
    import retry_circuit  # type: ignore[import-not-found,no-redef]

type Meta = dict[str, Any]

SCHEMA_VERSION = 1


def _source(task: Meta) -> Meta:
    return {
        "branch": str(task.get("observed_branch", "")),
        "checkpoint_commit": str(task.get("checkpoint_commit", "")),
        "head": str(task.get("observed_head", "")),
        "revision": int(task["task_revision"]),
    }


def _scope(task: Meta) -> tuple[Meta, list[Meta]]:
    history = change_drift.records(task)
    baseline = change_drift.latest(history, "baseline")
    declared = [] if baseline is None else change_drift.active_scope(history, baseline)
    outstanding = change_drift.unresolved(history)
    drift = [
        {
            "field": f"scope.{entry['path']}",
            "kind": "scope",
            "material": True,
            "status": "unacknowledged",
        }
        for entry in outstanding
    ]
    return {"declared": declared, "state": "absent" if baseline is None else "observed"}, drift


def _acknowledgements(task: Meta) -> list[Meta]:
    history = change_drift.records(task)
    delta = change_drift.latest(history, "delta")
    material = (
        {}
        if delta is None
        else {entry["path"] for entry in delta["payload"]["entries"] if entry["material"]}
    )
    scope = [
        {"field": f"scope.{path}", "kind": "scope", "record": "explain"}
        for record in history
        if record["kind"] == "explain"
        and record["payload"]["delta_digest"] == (delta or {}).get("digest")
        for path in record["payload"]["paths"]
        if path in material
    ]
    assumptions = [
        {"field": f"assumptions.{record['id']}", "kind": record["kind"], "record": record["action"]}
        for record in assumption_revalidation.records(task)
        if record["material"] and record["action"] in {"revalidate", "dispose"}
    ]
    return sorted(
        [*scope, *assumptions], key=lambda item: (str(item["field"]), str(item["record"]))
    )


def _assumptions(
    root: Path, projects_root: Path, task: Meta, tasks: list[Meta]
) -> tuple[list[Meta], list[Meta]]:
    cards: list[Meta] = []
    drift: list[Meta] = []
    for assumption_id, record in sorted(
        assumption_revalidation.active(assumption_revalidation.records(task)).items()
    ):
        item: Meta = {
            "id": assumption_id,
            "kind": record["kind"],
            "material": record["material"],
            "recorded_binding": record["binding"],
        }
        if record["action"] == "dispose":
            item["status"] = "disposed"
        else:
            try:
                current = assumption_revalidation.current_binding(
                    root, projects_root, task, tasks, record
                )
            except RuntimeError:
                item["status"] = "unavailable"
            else:
                item["status"] = "fresh" if current == record["binding"] else "stale"
                item["current_binding"] = current
        cards.append(item)
        if item["status"] in {"stale", "unavailable"}:
            drift.append(
                {
                    "field": f"assumptions.{assumption_id}",
                    "kind": str(record["kind"]),
                    "material": bool(record["material"]),
                    "status": item["status"],
                }
            )
    return cards, drift


def _evidence(task: Meta) -> Meta:
    acceptance = task.get("spec_acceptance")
    ledger = task.get("evidence_ledger")
    steps = ledger.get("steps", []) if isinstance(ledger, dict) else []
    return {
        "acceptance": acceptance if isinstance(acceptance, dict) else None,
        "predicates": [
            {
                key: step[key]
                for key in ("predicate_id", "status", "recorded_task_revision")
                if key in step
            }
            for step in sorted(steps, key=lambda value: str(value.get("predicate_id", "")))
            if isinstance(step, dict)
        ],
    }


def _retry(task: Meta) -> tuple[Meta, Meta | None]:
    state = task.get("retry_circuit")
    if state is None:
        return {"state": "clear"}, None
    checked = retry_circuit.validate(state)
    if checked["reservations"]:
        return {"state": "recovery_required", "reservations": len(checked["reservations"])}, {
            "kind": "recover-retry-reservation",
            "instruction": "Recover the live retry reservation before resuming work.",
        }
    if checked["hold"] is not None:
        return {"state": "held", "attempts": checked["hold"]["attempts"]}, {
            "kind": "recover-retry-circuit",
            "instruction": "Record one retry-circuit recovery before repeating the held command.",
        }
    return {"state": "clear", "attempts": len(checked["attempts"])}, None


def _next_action(task: Meta, drift: list[Meta], recovery: Meta | None) -> Meta:
    if recovery is not None:
        return recovery
    material = next((item for item in drift if item["material"]), None)
    if material is not None:
        if material["kind"] == "scope":
            return {
                "kind": "explain-material-drift",
                "instruction": "Explain the material scope drift.",
            }
        assumption_id = str(material["field"]).split(".", 1)[1]
        return {
            "kind": "revalidate-material-assumption",
            "instruction": f"Revalidate or explicitly dispose material assumption {assumption_id}.",
        }
    return {"kind": "task-next-action", "instruction": str(task["next_action"])}


def build(root: Path, projects_root: Path, task: Meta, tasks: list[Meta]) -> Meta:
    """Compose one stable card without changing authority or recording new data."""
    scope, scope_drift = _scope(task)
    assumptions, assumption_drift = _assumptions(root, projects_root, task, tasks)
    retry, recovery = _retry(task)
    drift = sorted([*scope_drift, *assumption_drift], key=lambda item: str(item["field"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "task": {
            "id": task["id"],
            "priority": task["priority"],
            "status": task["status"],
            "summary": task["summary"],
            "title": task["title"],
        },
        "source": _source(task),
        "scope": scope,
        "evidence": _evidence(task),
        "retry": retry,
        "assumptions": assumptions,
        "drift": drift,
        "acknowledgements": _acknowledgements(task),
        "next_action": _next_action(task, drift, recovery),
    }


def render_text(card: Meta) -> str:
    """Render the same card as compact, deterministic text-first output."""
    task = card["task"]
    lines = [
        f"RESUME_CARD={task['id']}",
        f"STATUS={task['status']}",
        f"SOURCE_HEAD={card['source']['head']}",
        f"SCOPE_STATE={card['scope']['state']}",
        f"RETRY_STATE={card['retry']['state']}",
        "DRIFT=" + json.dumps(card["drift"], sort_keys=True, separators=(",", ":")),
        "NEXT_ACTION=" + json.dumps(card["next_action"], sort_keys=True, separators=(",", ":")),
    ]
    return "\n".join(lines) + "\n"


def add_command_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the read-only resume-card command without lifecycle authority."""
    item = commands.add_parser("resume-card")
    item.add_argument("task")
    item.add_argument("--json", action="store_true")


def command(
    root: Path,
    projects_root: Path,
    task_id: str,
    tasks: list[tuple[Path, Meta, str]],
    as_json: bool,
) -> str:
    """Return one rendered card from a caller-held, consistent authority snapshot."""
    task = next((meta for _, meta, _ in tasks if meta.get("id") == task_id), None)
    if task is None:
        raise RuntimeError(f"unknown task {task_id}")
    card = build(root, projects_root, task, [meta for _, meta, _ in tasks])
    return (
        json.dumps(card, sort_keys=True, separators=(",", ":")) + "\n"
        if as_json
        else render_text(card)
    )
