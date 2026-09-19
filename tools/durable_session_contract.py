# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Pure validation for the disabled durable upgrade-session contract.

This module validates immutable session and outcome records only.  It does not
open stores, publish outcomes, or authorize any mutation or dispatch.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

CONTRACT_PATH = Path(__file__).parents[1] / "formal/upgrade/durable-upgrade-session-contract.json"
_IDENTITY_FIELDS = frozenset(
    {
        "authority_identity",
        "control_store_identity",
        "control_lock_identity",
        "journal_identity",
        "journal_bytes",
        "barrier_id",
        "fencing_owner",
        "fencing_token",
        "state_revision",
    }
)
_OUTCOME_FIELDS = frozenset(
    {"operation_id", "opcode", "outcome", "state_revision", "identity_digest"}
)
_OUTCOMES = frozenset({"success", "rejected", "ambiguous"})


class DurableSessionContractError(ValueError):
    """A session or outcome record violates the disabled contract."""


def load_contract(path: Path = CONTRACT_PATH) -> dict[str, Any]:
    """Load and validate the checked-in contract artifact."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DurableSessionContractError("durable session contract is unavailable") from error
    if not isinstance(value, dict):
        raise DurableSessionContractError("durable session contract must be an object")
    validate_contract(value)
    return value


def validate_contract(value: Mapping[str, Any]) -> None:
    """Reject contract drift that could accidentally authorize execution."""
    if value.get("kind") != "durable-upgrade-session-contract":
        raise DurableSessionContractError("durable session contract kind is invalid")
    if value.get("mutation_enabled") is not False or value.get("dispatch_enabled") is not False:
        raise DurableSessionContractError("durable session contract must remain disabled")
    _validate_safety(value)
    _validate_identity_fields(value)
    _validate_outcome_schema(value)


def _validate_safety(value: Mapping[str, Any]) -> None:
    if value.get("outcome_publication") != "disabled":
        raise DurableSessionContractError("durable outcome publication must remain disabled")
    if value.get("ambiguous_observation") != "safe_mode_and_reject":
        raise DurableSessionContractError("ambiguous observations must reject")


def _validate_identity_fields(value: Mapping[str, Any]) -> None:
    capture = value.get("capture")
    reread = value.get("assert_current")
    if not isinstance(capture, list) or not isinstance(reread, list):
        raise DurableSessionContractError("session identity fields are missing")
    if set(capture) != _IDENTITY_FIELDS or set(reread) != _IDENTITY_FIELDS:
        raise DurableSessionContractError("session identity fields are incomplete")


def _validate_outcome_schema(value: Mapping[str, Any]) -> None:
    outcome = value.get("outcome_record")
    if not isinstance(outcome, dict) or outcome.get("atomic") is not True:
        raise DurableSessionContractError("outcome record must be atomic")
    if set(outcome.get("required", ())) != _OUTCOME_FIELDS:
        raise DurableSessionContractError("outcome record fields are incomplete")
    if set(outcome.get("allowed", ())) != _OUTCOMES:
        raise DurableSessionContractError("outcome record outcomes are invalid")


def validate_snapshot(snapshot: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    """Require a complete, same-operation immutable session snapshot."""
    if set(snapshot) != _IDENTITY_FIELDS or set(expected) != _IDENTITY_FIELDS:
        raise DurableSessionContractError("session snapshot fields are incomplete")
    for field in _IDENTITY_FIELDS:
        value = snapshot[field]
        if field == "state_revision":
            if type(value) is not int or value < 0:
                raise DurableSessionContractError("session revision is invalid")
        elif not isinstance(value, str) or not value:
            raise DurableSessionContractError(f"session field is invalid: {field}")
        if value != expected[field]:
            raise DurableSessionContractError(f"session identity changed: {field}")


def validate_outcome(record: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    """Validate an outcome record without publishing or replaying it."""
    if set(record) != _OUTCOME_FIELDS:
        raise DurableSessionContractError("outcome record fields are incomplete")
    if set(expected) != _IDENTITY_FIELDS:
        raise DurableSessionContractError("expected session fields are incomplete")
    if not isinstance(record["operation_id"], str) or not record["operation_id"]:
        raise DurableSessionContractError("outcome operation is invalid")
    if not isinstance(record["opcode"], str) or not record["opcode"]:
        raise DurableSessionContractError("outcome opcode is invalid")
    if record["outcome"] not in _OUTCOMES:
        raise DurableSessionContractError("outcome value is invalid")
    if record["state_revision"] != expected["state_revision"]:
        raise DurableSessionContractError("outcome revision is stale")
    if record["identity_digest"] != expected["journal_identity"]:
        raise DurableSessionContractError("outcome identity is foreign")
