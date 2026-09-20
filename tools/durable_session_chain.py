# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Fail-closed identity and hash-chain contract for durable upgrade journals.

This module is intentionally independent of the executable upgrade engine.  It
provides a deterministic record format and replay validator so adapters can
prove a continuation before AR-0031 enables any mutation or dispatch.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

SCHEMA_VERSION = 1
GENESIS_DIGEST = None
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_STATUSES = frozenset({"open", "committed", "rolled_back", "ambiguous"})
_FSYNC_STATES = frozenset({"pending", "durable", "uncertain"})
FIELDS = (
    "schema_version",
    "session_id",
    "operation_id",
    "parent_operation_id",
    "journal_sequence",
    "previous_record_digest",
    "state_revision",
    "journal_identity",
    "authority_identity",
    "barrier_id",
    "fencing_owner",
    "fencing_token",
    "status",
    "fsync_state",
    "payload",
    "record_digest",
)
IMMUTABLE_SESSION_FIELDS = (
    "session_id",
    "journal_identity",
    "authority_identity",
    "barrier_id",
    "fencing_owner",
    "fencing_token",
)


class SessionChainError(ValueError):
    """A durable session record cannot be safely admitted or replayed."""


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise SessionChainError("session record is not canonical JSON") from error


def canonical_record_digest(record: Mapping[str, object]) -> str:
    """Hash a record excluding its self-referential digest."""
    if "record_digest" not in record:
        raise SessionChainError("session record digest is missing")
    unsigned = {field: record[field] for field in FIELDS if field != "record_digest"}
    return hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()


def _token(field: str, value: object, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise SessionChainError(f"session {field} is invalid")


def validate_record(record: Mapping[str, object]) -> dict[str, object]:  # noqa: C901
    """Validate one exact, deterministic session record without coercion."""
    if not isinstance(record, Mapping) or set(record) != set(FIELDS):
        raise SessionChainError("session record fields are invalid")
    if record["schema_version"] != SCHEMA_VERSION:
        raise SessionChainError("session record schema version is invalid")
    _token("session_id", record["session_id"])
    _token("operation_id", record["operation_id"])
    _token("parent_operation_id", record["parent_operation_id"], nullable=True)
    for field in (
        "journal_identity",
        "authority_identity",
        "barrier_id",
        "fencing_owner",
        "fencing_token",
    ):
        _token(field, record[field])
    for field in ("journal_sequence", "state_revision"):
        value = record[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise SessionChainError(f"session {field} is invalid")
    previous = record["previous_record_digest"]
    if previous is not None and (
        not isinstance(previous, str) or _DIGEST.fullmatch(previous) is None
    ):
        raise SessionChainError("session previous record digest is invalid")
    if record["status"] not in _STATUSES:
        raise SessionChainError("session status is invalid")
    if record["fsync_state"] not in _FSYNC_STATES:
        raise SessionChainError("session fsync state is invalid")
    if record["fsync_state"] == "uncertain" and record["status"] != "ambiguous":
        raise SessionChainError("uncertain fsync state must be permanently ambiguous")
    if not isinstance(record["payload"], Mapping):
        raise SessionChainError("session payload is invalid")
    digest = record["record_digest"]
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise SessionChainError("session record digest is invalid")
    if digest != canonical_record_digest(record):
        raise SessionChainError("session record digest does not match")
    return dict(record)


def validate_chain(records: Sequence[Mapping[str, object]]) -> tuple[dict[str, object], ...]:
    """Validate a complete chain; gaps, regressions, replacements and tamper fail."""
    validated: list[dict[str, object]] = []
    prior: dict[str, object] | None = None
    for expected_sequence, raw in enumerate(records, start=1):
        current = validate_record(raw)
        if current["journal_sequence"] != expected_sequence:
            raise SessionChainError("session journal sequence is not contiguous")
        if prior is None:
            if current["previous_record_digest"] is not GENESIS_DIGEST:
                raise SessionChainError("first session record must use the genesis digest")
        elif current["previous_record_digest"] != prior["record_digest"]:
            raise SessionChainError("session hash chain is broken")
        if prior is not None:
            for field in IMMUTABLE_SESSION_FIELDS:
                if current[field] != prior[field]:
                    raise SessionChainError(f"session identity changed: {field}")
        prior = current
        validated.append(current)
    return tuple(validated)


def reconcile_replay(existing: Mapping[str, object], incoming: Mapping[str, object]) -> str:
    """Return ``exact`` only for byte-equivalent replay; otherwise reject."""
    left = validate_record(existing)
    right = validate_record(incoming)
    if left["journal_sequence"] != right["journal_sequence"]:
        raise SessionChainError("replayed session sequence does not match")
    if _canonical_bytes(left) != _canonical_bytes(right):
        raise SessionChainError("conflicting duplicate session record")
    return "exact"


@dataclass(frozen=True, slots=True)
class DurableSessionChain:
    """Immutable validated chain exposed to read-only durable adapters."""

    records: tuple[Mapping[str, object], ...]

    @classmethod
    def load(cls, records: Sequence[Mapping[str, object]]) -> DurableSessionChain:
        return cls(validate_chain(records))

    @property
    def terminal(self) -> Mapping[str, object] | None:
        return self.records[-1] if self.records else None

    def append(self, record: Mapping[str, object]) -> DurableSessionChain:
        """Validate a continuation without performing persistence or dispatch."""
        return type(self).load((*self.records, record))
