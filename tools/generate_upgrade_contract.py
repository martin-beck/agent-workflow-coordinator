# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Generate a deterministic, data-only release upgrade contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__:
    from .validate_upgrade_contract import ContractError, validate_contract
else:  # pragma: no cover - direct script execution
    from validate_upgrade_contract import (  # type: ignore[import-not-found,no-redef]
        ContractError,
        validate_contract,
    )

PHASES = ("discover", "preflight", "quiesce", "backup", "stage", "commit", "validate", "reopen")


def _validate_transition(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("transition input must be an object")
    if set(value) != {"operation_id", "from", "to"}:
        raise ContractError("transition input must contain only operation_id, from, and to")
    if not isinstance(value["operation_id"], str):
        raise ContractError("operation_id must be a string")
    if not isinstance(value["from"], dict) or not isinstance(value["to"], dict):
        raise ContractError("from and to must be release objects")
    return value


def _load_transition(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"invalid transition input: {error}") from error
    return _validate_transition(value)


def _operation(operation_id: str, phase: str) -> dict[str, Any]:
    return {
        "operation_id": f"{operation_id}:{phase}",
        "timeout_seconds": 300,
        "resources": ["maintenance-barrier", "durable-operation-record"],
        "preconditions": ["previous-phase-complete"],
        "postconditions": [f"{phase}-contract-satisfied"],
        "evidence": ["durable-operation-record"],
        "durable_record": "operation-id-and-outcome",
    }


def generate(transition: dict[str, Any]) -> dict[str, Any]:
    operation_id = transition["operation_id"]
    phases = []
    dependencies = {
        "discover": [],
        "preflight": ["discover"],
        "quiesce": ["preflight"],
        "backup": ["quiesce"],
        "stage": ["backup"],
        "commit": ["quiesce", "backup", "stage"],
        "validate": ["commit"],
        "reopen": ["validate"],
    }
    failure_modes = {
        "discover": "stop-before-mutation",
        "preflight": "stop-before-mutation",
        "quiesce": "stop-before-mutation",
        "backup": "stop-before-mutation",
        "stage": "restore-known-good",
        "commit": "restore-known-good",
        "validate": "restore-known-good",
        "reopen": "safe-mode",
    }
    for order, phase in enumerate(PHASES, 1):
        phases.append(
            {
                "id": phase,
                "order": order,
                "mutates_authority": phase == "commit",
                "requires": dependencies[phase],
                "on_failure": failure_modes[phase],
                "operation": _operation(operation_id, phase),
            }
        )
    document = {
        "schema_version": 1,
        "operation_id": operation_id,
        "from": transition["from"],
        "to": transition["to"],
        "preconditions": [
            {
                "id": "RELEASE.AUTHENTICITY",
                "effect": "read-only",
                "failure_mode": "stop-before-mutation",
                "preconditions": ["immutable-tag-and-commit", "trusted-signature"],
                "postconditions": ["release-identity-recorded"],
                "evidence": ["release-manifest", "signature-verification"],
            },
            {
                "id": "BACKEND.COMPATIBILITY",
                "effect": "validate",
                "failure_mode": "stop-before-mutation",
                "preconditions": ["backend-contracts-present", "schema-compatible"],
                "postconditions": ["backend-round-trip-plan-recorded"],
                "evidence": ["compatibility-matrix"],
            },
        ],
        "phases": phases,
        "backend_contracts": [
            {
                "backend": "git",
                "authority": ["reachable-objects", "refs", "index", "task-history"],
                "backup": ["object-and-ref-inventory", "binding", "projections"],
                "restore": ["verify-objects", "restore-refs", "verify-history"],
                "selector": ["coordinator.backend.json"],
                "projections": ["CURRENT.md", "STATUS.md"],
                "equivalence": "authority-compatible-round-trip",
            },
            {
                "backend": "sqlite",
                "authority": ["database", "wal", "shm"],
                "backup": ["online-backup-api", "binding", "projections"],
                "restore": ["integrity-check", "restore-selector", "verify-history"],
                "selector": ["coordinator.backend.json"],
                "projections": ["CURRENT.md", "STATUS.md"],
                "equivalence": "authority-compatible-round-trip",
                "wal": ["checkpoint-policy", "synchronous-full"],
            },
        ],
        "rollback": {
            "required": True,
            "backup_integrity": "backend-specific",
            "integrity_by_backend": {
                "git": "git-object-and-ref",
                "sqlite": "sqlite-integrity-and-backup-api",
            },
            "equivalence": "authority-compatible-round-trip",
            "reopen_gate": "validate-before-reopen",
            "ambiguous_external_result": "persist-operation-id-and-reconcile",
        },
    }
    validate_contract(document)
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    try:
        document = generate(_load_transition(args.input))
        args.output.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (ContractError, OSError) as error:
        print(f"invalid upgrade transition: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
