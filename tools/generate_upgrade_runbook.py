# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Generate deterministic, privacy-safe operator and agent upgrade runbooks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .validate_upgrade_contract import ContractError, validate_contract


class RunbookError(ValueError):
    """Raised when a runbook cannot be generated safely."""


def _read_contract(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RunbookError("contract is unavailable or invalid JSON") from error
    if not isinstance(document, dict):
        raise RunbookError("contract must be an object")
    try:
        validate_contract(document)
    except ContractError as error:
        raise RunbookError("contract validation failed") from error
    return document


def _phase_rows(document: dict[str, Any]) -> list[str]:
    return [
        f"| {phase['order']} | `{phase['id']}` | `{phase['operation']['opcode']}` | "
        f"{phase['on_failure']} |"
        for phase in document["phases"]
    ]


def _backend_steps(backend: str) -> list[str]:
    if backend == "git":
        return [
            (
                "Confirm reachable objects, refs, index metadata, binding, projections, "
                "and task history are included."
            ),
            "Record object/ref inventory and verify restore-equivalent history before replacement.",
            "After restore, verify objects, refs, projections, and authority-compatible history.",
        ]
    return [
        (
            "Use the online SQLite backup API (or a clean checkpoint); never copy only "
            "the main database while writers are active."
        ),
        (
            "Retain the database, WAL/SHM companions, binding, selector, and projections "
            "in the backup record."
        ),
        (
            "After restore, run integrity and authority-compatible round-trip checks, "
            "including WAL/SHM identity checks."
        ),
    ]


def generate_runbooks(document: dict[str, Any]) -> dict[str, str]:
    """Return public operator and agent runbooks for one validated contract."""
    try:
        validate_contract(document)
    except ContractError as error:
        raise RunbookError("contract validation failed") from error
    operation_id = document["operation_id"]
    backend = document["backend"]
    source = document["from"]["version"]
    target = document["to"]["version"]
    phases = "\n".join(_phase_rows(document))
    backend_steps = "\n".join(f"- {step}" for step in _backend_steps(backend))
    operator = f"""# Upgrade runbook: {source} to {target}

This runbook is generated from a validated release contract. It describes a
bounded procedure; it is not evidence that the upgrade is executable or that
the coordinator is healthy.

## Approval boundary

An operator must approve the exact release pair and operation `{operation_id}`
after reviewing the generated contract and its immutable release evidence.
The coordinator remains work closed from quiescence through validation. Do not
approve a retry for an interrupted external operation; reconcile its durable
operation record first.

## Procedure

1. Run the read-only contract check and confirm its result is valid and not executable.
2. Run every phase in the order below, recording each durable operation outcome.
3. Acquire and retain the maintenance barrier through replacement and validation.
4. Do not reopen work unless validation succeeds and the known release identity is confirmed.

| Order | Phase | Operation | Failure action |
| ---: | --- | --- | --- |
{phases}

## Backup and rollback

Before staging or replacement, verify a complete `{backend}` backup and its
restore-equivalence evidence. The backup must remain available until reopen is
validated. On any post-backup failure, restore the known-good release and
authority, validate that restoration, and only then reopen. If restoration or
validation is ambiguous, preserve the barrier and enter safe mode with work
closed; operator inspection is required. The contract's rollback operation is
`backend.restore`; it is not an invitation to invent an alternate command.

{backend_steps}

## Interruption

After a process or host failure, inspect live holders, the durable operation
record, release selector, authority revision, and fencing identity. reconcile
the recorded operation exactly once. A successful health check alone does not
prove recoverability, backup completeness, release correctness, or rollback
equivalence.
"""
    agent = f"""# Agent runbook: {source} to {target}

Generated operation: `{operation_id}`  
Selected backend: `{backend}`

## Non-negotiable rules

- Treat the validated contract as the only source of phase order and operation IDs.
- Keep work closed while the barrier is held; every mutating action carries its fence.
- Never invent a missing prerequisite, identity, backup result, or operation outcome.
- Never retry an interrupted external action before durable reconciliation.
- A health check is an observation only; it does not establish recoverability or correctness.

## Agent execution loop

1. Inspect and validate the contract without mutation.
2. For each phase, verify predecessor completion, exact operation identity, and durable evidence.
3. Stop before mutation when a prerequisite, identity, or backup check fails.
4. After replacement, validate release, authority, backend, projections, revisions, and
   fencing state.
5. Reopen only after validation; otherwise restore and validate the known-good state or
   record safe mode.

## Backend evidence

{backend_steps}

## Failure and recovery

Record the failure against the current operation ID. Preserve old and staged
release identities, the barrier, backup, and journal. reconcile only to a
validated old state, validated new state, or explicit safe mode. Do not
claim completion from partial logs, process liveness, or a passing health check.
"""
    return {"operator.md": operator, "agent.md": agent}


def write_runbooks(document: dict[str, Any], output: Path) -> tuple[Path, Path]:
    """Write the two generated runbooks below an existing or new directory."""
    runbooks = generate_runbooks(document)
    output.mkdir(parents=True, exist_ok=True)
    paths: tuple[Path, Path] = (output / "operator.md", output / "agent.md")
    for path, name in zip(paths, runbooks, strict=True):
        path.write_text(runbooks[name], encoding="utf-8")
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contract", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    try:
        write_runbooks(_read_contract(args.contract), args.output)
    except (OSError, RunbookError, ContractError) as error:
        print(f"unable to generate upgrade runbooks: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
