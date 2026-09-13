# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Fail-closed semantic validation for generated upgrade contracts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
PHASES = ("discover", "preflight", "quiesce", "backup", "stage", "commit", "validate", "reopen")


class ContractError(ValueError):
    """Raised when a contract is structurally or semantically unsafe."""


def validate_contract(document: dict[str, Any]) -> None:
    """Validate a complete generated contract before any upgrade mutation."""
    schema = json.loads((ROOT / "schema/upgrade-contract.schema.json").read_text())
    Draft202012Validator(schema).validate(document)
    if document["from"] == document["to"]:
        raise ContractError("from and to release identities must differ")
    _validate_phases(document)
    _validate_backends(document)


def _validate_phases(document: dict[str, Any]) -> None:
    """Validate ordering, dependencies, operation identity, and gates."""
    phases = document["phases"]
    if [phase["id"] for phase in phases] != list(PHASES):
        raise ContractError("phase IDs must be unique and in canonical order")
    if [phase["order"] for phase in phases] != list(range(1, len(PHASES) + 1)):
        raise ContractError("phase orders must be contiguous from one")
    by_id = {phase["id"]: phase["order"] for phase in phases}
    operation_ids: set[str] = set()
    top_operation_id = document["operation_id"]
    for phase in phases:
        phase_id = phase["id"]
        for dependency in phase["requires"]:
            if dependency not in by_id or by_id[dependency] >= phase["order"]:
                raise ContractError(f"invalid dependency {dependency!r} for {phase_id}")
        operation_id = phase["operation"]["operation_id"]
        expected = f"{top_operation_id}:{phase_id}"
        if operation_id != expected or operation_id in operation_ids:
            raise ContractError(f"operation ID is not bound to {phase_id}")
        operation_ids.add(operation_id)
    commit = phases[5]
    if not commit["mutates_authority"] or not {"stage", "quiesce", "backup"} <= set(
        commit["requires"]
    ):
        raise ContractError(
            "commit must be the only authority mutation and require stage/quiesce/backup"
        )
    if set(phases[7]["requires"]) != {"validate"}:
        raise ContractError("reopen must require validate")


def _validate_backends(document: dict[str, Any]) -> None:
    """Validate backend coverage and backend-specific rollback integrity."""
    if {backend["backend"] for backend in document["backend_contracts"]} != {"git", "sqlite"}:
        raise ContractError("both Git and SQLite backend contracts are required")
    integrity = document["rollback"]["integrity_by_backend"]
    if integrity != {"git": "git-object-and-ref", "sqlite": "sqlite-integrity-and-backup-api"}:
        raise ContractError("rollback integrity must match each backend")


def main(argv: list[str] | None = None) -> int:
    """Validate one contract file and return a stable CLI status."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: validate_upgrade_contract.py CONTRACT.json", file=sys.stderr)
        return 2
    try:
        validate_contract(json.loads(Path(args[0]).read_text()))
    except (ContractError, json.JSONDecodeError, OSError) as error:
        print(f"invalid upgrade contract: {error}", file=sys.stderr)
        return 1
    print("upgrade contract: valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
