# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Pure validation for the disabled durable upgrade-session contract.

This module validates immutable session and outcome records only.  It does not
open stores, publish outcomes, or authorize any mutation or dispatch.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
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
_JOURNAL_FIELDS = frozenset(
    {"operation_id", "state_revision", "journal_identity", "status", "fsync"}
)
_JOURNAL_STATUSES = frozenset({"captured", "ambiguous"})
_JOURNAL_FSYNC = frozenset({"durable", "uncertain"})
_JOURNAL_ENVELOPE_FIELDS = frozenset({"schema_version", "kind", "record"})
_TRANSACTION_FIELDS = frozenset(
    {"operation_id", "sequence", "intent", "state", "state_revision", "journal_identity", "fsync"}
)
_TRANSACTION_INTENTS = frozenset({"append", "replay"})
_TRANSACTION_STATES = frozenset({"captured", "terminal", "rollback_required", "ambiguous"})


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
    _validate_journal_schema(value)


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


def _validate_journal_schema(value: Mapping[str, Any]) -> None:
    journal = value.get("journal_record")
    if not isinstance(journal, dict) or journal.get("atomic") is not True:
        raise DurableSessionContractError("journal record must be atomic")
    if set(journal.get("required", ())) != _JOURNAL_FIELDS:
        raise DurableSessionContractError("journal record fields are incomplete")
    if set(journal.get("allowed_status", ())) != _JOURNAL_STATUSES:
        raise DurableSessionContractError("journal record statuses are invalid")
    if set(journal.get("allowed_fsync", ())) != _JOURNAL_FSYNC:
        raise DurableSessionContractError("journal record fsync values are invalid")


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


def validate_journal_record(record: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    """Validate an atomic journal observation without persisting or replaying it."""
    if set(record) != _JOURNAL_FIELDS:
        raise DurableSessionContractError("journal record fields are incomplete")
    if set(expected) != _IDENTITY_FIELDS:
        raise DurableSessionContractError("expected session fields are incomplete")
    if not isinstance(record["operation_id"], str) or not record["operation_id"]:
        raise DurableSessionContractError("journal operation is invalid")
    if record["state_revision"] != expected["state_revision"]:
        raise DurableSessionContractError("journal revision is stale")
    if record["journal_identity"] != expected["journal_identity"]:
        raise DurableSessionContractError("journal identity is foreign")
    status = record["status"]
    fsync = record["fsync"]
    if status not in _JOURNAL_STATUSES:
        raise DurableSessionContractError("journal status is invalid")
    if fsync not in _JOURNAL_FSYNC:
        raise DurableSessionContractError("journal fsync value is invalid")
    if (status, fsync) not in {("captured", "durable"), ("ambiguous", "uncertain")}:
        raise DurableSessionContractError("journal durability is ambiguous")


def serialize_journal_record(record: Mapping[str, Any], expected: Mapping[str, Any]) -> bytes:
    """Encode one validated journal record without writing it anywhere."""
    validate_journal_record(record, expected)
    envelope = {"kind": "durable-journal-record", "record": dict(record), "schema_version": 1}
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def deserialize_journal_record(payload: bytes, expected: Mapping[str, Any]) -> dict[str, Any]:
    """Decode and validate a canonical journal envelope, rejecting truncation/drift."""
    if not isinstance(payload, bytes):
        raise DurableSessionContractError("journal envelope must be bytes")
    try:
        envelope = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DurableSessionContractError("journal envelope is malformed") from error
    if not isinstance(envelope, dict) or set(envelope) != _JOURNAL_ENVELOPE_FIELDS:
        raise DurableSessionContractError("journal envelope fields are invalid")
    if envelope["schema_version"] != 1 or envelope["kind"] != "durable-journal-record":
        raise DurableSessionContractError("journal envelope schema is invalid")
    record = envelope["record"]
    if not isinstance(record, dict):
        raise DurableSessionContractError("journal envelope record is invalid")
    validate_journal_record(record, expected)
    return dict(record)


def reconcile_journal_envelopes(
    payloads: Sequence[bytes], expected: Mapping[str, Any]
) -> dict[str, Any]:
    """Reconcile canonical envelope rereads without writing or replaying them."""
    if not payloads:
        raise DurableSessionContractError("journal envelopes are missing")
    records = tuple(deserialize_journal_record(payload, expected) for payload in payloads)
    return reconcile_journal_records(records, expected)


def _validate_transaction_record(
    record: Mapping[str, Any], expected: Mapping[str, Any], sequence: int
) -> dict[str, Any]:
    if set(record) != _TRANSACTION_FIELDS:
        raise DurableSessionContractError("journal transaction fields are incomplete")
    if type(record["sequence"]) is not int or record["sequence"] != sequence:
        raise DurableSessionContractError("journal transaction sequence is discontinuous")
    if record["intent"] not in _TRANSACTION_INTENTS:
        raise DurableSessionContractError("journal transaction intent is invalid")
    if record["state"] not in _TRANSACTION_STATES:
        raise DurableSessionContractError("journal transaction state is invalid")
    if record["state_revision"] != expected["state_revision"]:
        raise DurableSessionContractError("journal transaction revision is stale")
    if record["journal_identity"] != expected["journal_identity"]:
        raise DurableSessionContractError("journal transaction identity is foreign")
    if record["state"] == "ambiguous" or record["fsync"] == "uncertain":
        raise DurableSessionContractError("ambiguous journal transaction requires safe mode")
    if record["fsync"] != "durable":
        raise DurableSessionContractError("journal transaction fsync is invalid")
    return dict(record)


def validate_journal_transaction(
    records: Sequence[Mapping[str, Any]], expected: Mapping[str, Any]
) -> tuple[dict[str, Any], ...]:
    """Validate contiguous append/replay state without persisting or executing it."""
    if not records:
        raise DurableSessionContractError("journal transaction is missing")
    normalized: list[dict[str, Any]] = []
    terminal_seen = False
    for sequence, record in enumerate(records):
        current = _validate_transaction_record(record, expected, sequence)
        if terminal_seen:
            raise DurableSessionContractError("journal transaction continues after terminal state")
        terminal_seen = current["state"] == "terminal"
        normalized.append(current)
    return tuple(normalized)


def classify_journal_recovery(
    records: Sequence[Mapping[str, Any]], expected: Mapping[str, Any]
) -> str:
    """Classify a validated transaction without resuming or rolling anything back."""
    validated = validate_journal_transaction(records, expected)
    terminal = validated[-1]["state"]
    if terminal == "terminal":
        return "terminal"
    if terminal == "rollback_required":
        raise DurableSessionContractError("rollback-required state requires safe mode")
    return "resume"


def reconcile_journal_records(
    observations: Sequence[Mapping[str, Any]], expected: Mapping[str, Any]
) -> dict[str, Any]:
    """Return one stable observation, or reject uncertain/replaced journal state.

    This is intentionally an in-memory checker. It performs no journal writes,
    outcome publication, recovery, or mutation; an ambiguous observation is a
    permanent safe-mode rejection for the caller.
    """
    if not observations:
        raise DurableSessionContractError("journal observations are missing")
    normalized: list[dict[str, Any]] = []
    for observation in observations:
        validate_journal_record(observation, expected)
        normalized.append(dict(observation))
    if any(observation["status"] == "ambiguous" for observation in normalized):
        raise DurableSessionContractError("ambiguous journal state requires safe mode")
    first = normalized[0]
    if any(observation != first for observation in normalized[1:]):
        raise DurableSessionContractError("journal observation changed")
    return first


class InMemoryJournalRecordStore:
    """Append-idempotent, non-persistent journal adapter for contract tests.

    The store intentionally has no filesystem, SQLite, backend, or dispatch
    dependency.  Ambiguous observations permanently fence this instance.
    """

    def __init__(self, expected: Mapping[str, Any]) -> None:
        if set(expected) != _IDENTITY_FIELDS:
            raise DurableSessionContractError("expected session fields are incomplete")
        self._expected = dict(expected)
        self._records: dict[tuple[str, int, str], dict[str, Any]] = {}
        self._safe_mode = False

    @property
    def safe_mode(self) -> bool:
        return self._safe_mode

    def append(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Append a durable record, accepting only exact idempotent retries."""
        if self._safe_mode:
            raise DurableSessionContractError("journal store is in safe mode")
        validate_journal_record(record, self._expected)
        if record["status"] != "captured" or record["fsync"] != "durable":
            self._safe_mode = True
            raise DurableSessionContractError("journal append requires durable capture")
        key = (record["operation_id"], record["state_revision"], record["journal_identity"])
        existing = self._records.get(key)
        if existing is not None and existing != dict(record):
            self._safe_mode = True
            raise DurableSessionContractError("journal replay conflicts with existing record")
        stored = dict(record)
        self._records[key] = stored
        return dict(stored)

    def observe(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Classify a reread; uncertainty fences before any append is attempted."""
        if self._safe_mode:
            raise DurableSessionContractError("journal store is in safe mode")
        validate_journal_record(record, self._expected)
        if record["status"] == "ambiguous" or record["fsync"] == "uncertain":
            self._safe_mode = True
            raise DurableSessionContractError("ambiguous journal observation requires safe mode")
        return self.append(record)

    def records(self) -> tuple[dict[str, Any], ...]:
        """Return a detached, deterministic view for read-only assertions."""
        return tuple(dict(record) for record in self._records.values())
