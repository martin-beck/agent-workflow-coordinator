# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Durable fail-closed phase journal for the upgrade engine."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, cast

from tools.upgrade_admission import (
    admit_preflight,
    admit_quiesced,
    admit_reopen,
    recheck_before_replacement,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - the coordinator is POSIX-only
    fcntl = None  # type: ignore[assignment]

PHASES = ("discover", "preflight", "quiesce", "backup", "stage", "commit", "validate", "reopen")
MAX_OPERATION_ID_LENGTH = 128 - max(len(f".{phase}") for phase in (*PHASES, "rollback"))


class UpgradeError(RuntimeError):
    """An upgrade cannot safely advance."""


Handler = Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None]


class BackendAdapter(Protocol):
    """Concrete authority adapter for Git or SQLite upgrade operations."""

    def snapshot(self, phase: str, context: Mapping[str, object]) -> Mapping[str, object]: ...

    def verify_rollback_context(self, context: Mapping[str, object]) -> bool: ...

    def execute(self, phase: str, context: Mapping[str, object]) -> Mapping[str, object]: ...


REQUIRED_EVIDENCE = {
    "discover": ("release_authentic", "runtime_supported", "backend_identity_verified"),
    "preflight": ("preflight_admitted", "capacity_verified", "backend_identity_verified"),
    "quiesce": ("barrier_acquired", "workers_drained", "leases_fenced", "fencing_verified"),
    "backup": ("backup_verified", "restore_roundtrip_verified", "backend_identity_verified"),
    "stage": ("staged_verified", "manifest_verified", "backend_identity_verified"),
    "commit": (
        "quiesced",
        "backup_verified",
        "selector_verified",
        "selector_commit_atomic",
        "fencing_verified",
        "selector_before_verified",
        "selector_after_verified",
    ),
    "validate": (
        "runtime_validated",
        "backend_roundtrip_valid",
        "projections_valid",
        "binding_valid",
    ),
    "reopen": ("validated", "barrier_held"),
}
PHASE_MUTATION = {phase: phase == "commit" for phase in PHASES}
STATUSES = {"planned", "running", "failed", "completed", "rolled-back", "safe-mode"}
TOP_LEVEL_FIELDS = {"schema_version", "operation_id", "status", "phase", "context", "records"}
RECORD_FIELDS = {"operation_id", "step_id", "phase", "outcome", "result", "error", "context"}
CONTEXT_FIELDS = (
    "operation_id",
    "project_id",
    "state_revision",
    "fencing_token",
    "fencing_owner",
    "backend",
    "authority_revision",
    "durable_barrier_id",
    "barrier_identity_digest",
    "envelope_digest",
    "target",
)


@dataclass(frozen=True)
class PhaseContext:
    """Immutable identity and backend binding for one upgrade operation."""

    operation_id: str
    project_id: str
    state_revision: int
    fencing_token: str
    fencing_owner: str
    backend: str
    authority_revision: str
    durable_barrier_id: str
    barrier_identity_digest: str
    envelope_digest: str
    target: str


def _freeze(value: object) -> object:
    """Create a recursively immutable view for untrusted phase handlers."""
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as cleanup_error:
            raise UpgradeError("durable upgrade journal cleanup failed") from cleanup_error
        raise UpgradeError("durable upgrade journal write failed") from error


def _validate_context_identities(supplied: dict[str, object]) -> None:
    if any(
        not isinstance(supplied[field], str) or not supplied[field]
        for field in CONTEXT_FIELDS
        if field != "state_revision"
    ):
        raise UpgradeError("invalid phase context identity")
    for field in ("barrier_identity_digest", "envelope_digest"):
        if not re.fullmatch(r"[0-9a-f]{64}", cast(str, supplied[field])):
            raise UpgradeError(f"invalid {field}")
    for field in (
        "operation_id",
        "fencing_token",
        "fencing_owner",
        "authority_revision",
        "durable_barrier_id",
    ):
        limit = MAX_OPERATION_ID_LENGTH if field == "operation_id" else 127
        if not re.fullmatch(
            rf"[A-Za-z0-9][A-Za-z0-9._-]{{0,{limit - 1}}}", cast(str, supplied[field])
        ):
            raise UpgradeError(f"invalid {field}")
    try:
        project = uuid.UUID(cast(str, supplied["project_id"]))
    except ValueError as error:
        raise UpgradeError("invalid project_id") from error
    if project.version != 4:
        raise UpgradeError("project_id must be UUIDv4")
    if supplied["backend"] not in {"git", "sqlite"} or supplied["target"] not in {
        "new",
        "rollback",
    }:
        raise UpgradeError("unsupported backend or target")


def _validate_context(supplied: dict[str, object], operation_id: str) -> None:
    if set(supplied) != set(CONTEXT_FIELDS) or supplied.get("operation_id") != operation_id:
        raise UpgradeError("complete bound phase context is required")
    if (
        not isinstance(supplied["state_revision"], int)
        or isinstance(supplied["state_revision"], bool)
        or supplied["state_revision"] < 1
    ):
        raise UpgradeError("invalid state revision")
    _validate_context_identities(supplied)


class UpgradeEngine:
    """Execute exactly eight ordered phases with durable outcomes."""

    def __init__(
        self,
        operation_id: str,
        journal: Path,
        context: Mapping[str, object],
        lock_path: Path | None = None,
        backend_adapter: BackendAdapter | None = None,
    ) -> None:
        if not operation_id or ":" in operation_id:
            raise UpgradeError("invalid operation identity")
        self.operation_id = operation_id
        self.journal = journal
        self.lock_path = lock_path or journal.parent / ".upgrade-engine.lock"
        self.backend_adapter = backend_adapter
        self._verified_rollback_context: dict[str, object] | None = None
        supplied = dict(context)
        _validate_context(supplied, operation_id)
        self.context = PhaseContext(**cast(dict[str, Any], supplied))

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Serialize journal decisions and fail closed if locking is unavailable."""
        if fcntl is None:
            raise UpgradeError("upgrade lock is unavailable")
        lock = self.lock_path
        lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with lock.open("a+") as stream:
            try:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise UpgradeError("upgrade lock acquisition timed out") from None
                        time.sleep(0.01)
            except OSError as error:
                raise UpgradeError("upgrade lock acquisition failed") from error
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def check(self) -> dict[str, object]:
        return {"operation_id": self.operation_id, "phases": list(PHASES), "checked": True}

    def plan(self) -> dict[str, Any]:
        with self._exclusive():
            if self.journal.exists():
                raise UpgradeError("operation already planned")
            value: dict[str, Any] = {
                "schema_version": 1,
                "operation_id": self.operation_id,
                "status": "planned",
                "phase": None,
                "context": asdict(self.context),
                "records": [],
            }
            _write(self.journal, value)
            return value

    def _load(self) -> dict[str, Any]:  # noqa: C901
        try:
            value = json.loads(self.journal.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise UpgradeError("upgrade journal is unreadable") from error
        if (
            value.get("schema_version") != 1
            or set(value) != TOP_LEVEL_FIELDS
            or value.get("operation_id") != self.operation_id
            or value.get("status") not in STATUSES
        ):
            raise UpgradeError("upgrade journal identity or records are invalid")
        context = value.get("context")
        if not isinstance(context, dict) or context != asdict(self.context):
            raise UpgradeError("upgrade journal context is invalid or changed")
        records = value.get("records")
        if not isinstance(records, list):
            raise UpgradeError("upgrade journal records are invalid")
        expected = 0
        phase_outcomes: list[str] = []
        rollback_seen = False
        for record in records:
            if not isinstance(record, dict):
                raise UpgradeError("upgrade journal record is invalid")
            phase = record.get("phase")
            if phase == "rollback":
                if rollback_seen:
                    raise UpgradeError("duplicate rollback record")
                rollback_seen = True
                if (
                    record.get("operation_id") != self.operation_id
                    or record.get("step_id") != f"{self.operation_id}.rollback"
                ):
                    raise UpgradeError("upgrade journal rollback identity is invalid")
                if record.get("outcome") not in {"started", "rollback_completed", "ambiguous"}:
                    raise UpgradeError("upgrade journal rollback outcome is invalid")
                rollback_context = record.get("context")
                if not isinstance(rollback_context, dict):
                    raise UpgradeError("verified rollback context is required")
                rollback_context = dict(rollback_context)
                _validate_context(rollback_context, self.operation_id)
                if rollback_context["target"] != "rollback":
                    raise UpgradeError("rollback context target is invalid")
                for field in CONTEXT_FIELDS:
                    if field not in {"target", "barrier_identity_digest", "envelope_digest"} and (
                        rollback_context[field] != asdict(self.context)[field]
                    ):
                        raise UpgradeError(f"rollback context mismatch: {field}")
                verifier = getattr(self.backend_adapter, "verify_rollback_context", None)
                if not callable(verifier) or verifier(rollback_context) is not True:
                    raise UpgradeError("rollback context is not verified by authority")
                if set(record) - RECORD_FIELDS:
                    raise UpgradeError("upgrade journal rollback context is invalid")
                outcome = record["outcome"]
                expected_fields = {
                    "started": {"operation_id", "step_id", "phase", "outcome", "context"},
                    "rollback_completed": {
                        "operation_id",
                        "step_id",
                        "phase",
                        "outcome",
                        "context",
                        "result",
                    },
                    "ambiguous": {
                        "operation_id",
                        "step_id",
                        "phase",
                        "outcome",
                        "context",
                        "error",
                    },
                }[outcome]
                if set(record) != expected_fields:
                    raise UpgradeError("rollback record fields are invalid")
                if outcome == "rollback_completed" and not isinstance(record["result"], dict):
                    raise UpgradeError("successful rollback lacks result evidence")
                if outcome == "ambiguous" and not isinstance(record["error"], str):
                    raise UpgradeError("ambiguous rollback lacks error evidence")
                continue
            if phase is not None:
                if rollback_seen:
                    raise UpgradeError("phase follows rollback record")
                if (
                    expected >= len(PHASES)
                    or phase != PHASES[expected]
                    or record.get("operation_id") != self.operation_id
                    or record.get("step_id") != f"{self.operation_id}.{phase}"
                ):
                    raise UpgradeError("upgrade journal phase identity is invalid")
                if not isinstance(record.get("step_id"), str) or len(record["step_id"]) > 128:
                    raise UpgradeError("upgrade journal step identity is invalid")
                if record.get("outcome") not in {"started", "success", "failed", "ambiguous"}:
                    raise UpgradeError("upgrade journal outcome is invalid")
                if set(record) - RECORD_FIELDS or record.get("context") != asdict(self.context):
                    raise UpgradeError("upgrade journal record fields are invalid")
                outcome = record["outcome"]
                expected_fields = {
                    "started": {"operation_id", "step_id", "phase", "outcome", "context"},
                    "success": {"operation_id", "step_id", "phase", "outcome", "context", "result"},
                    "failed": {"operation_id", "step_id", "phase", "outcome", "context", "error"},
                    "ambiguous": {
                        "operation_id",
                        "step_id",
                        "phase",
                        "outcome",
                        "context",
                        "error",
                    },
                }[outcome]
                if set(record) != expected_fields:
                    raise UpgradeError("phase outcome fields are invalid")
                if outcome == "success" and not isinstance(record.get("result"), dict):
                    raise UpgradeError("successful phase lacks result evidence")
                if outcome in {"failed", "ambiguous"} and not isinstance(record.get("error"), str):
                    raise UpgradeError("failed phase lacks error evidence")
                if outcome == "started" and set(record) != {
                    "operation_id",
                    "step_id",
                    "phase",
                    "outcome",
                    "context",
                }:
                    raise UpgradeError("started phase has terminal evidence")
                phase_outcomes.append(cast(str, record["outcome"]))
                expected += 1
            else:
                raise UpgradeError("upgrade journal record phase is missing")
        status = cast(str, value["status"])
        if status == "planned" and records:
            raise UpgradeError("planned journal contains records")
        if status == "planned" and value.get("phase") is not None:
            raise UpgradeError("planned journal has an active phase")
        if status == "running" and (
            not phase_outcomes or phase_outcomes[-1] not in {"started", "success"}
        ):
            raise UpgradeError("running journal is not recoverable")
        if status == "running" and value.get("phase") != PHASES[len(phase_outcomes) - 1]:
            raise UpgradeError("running journal phase is inconsistent")
        if status in {"failed", "safe-mode"} and not phase_outcomes:
            raise UpgradeError("failed journal has no failed phase")
        rollback_records = [record for record in records if record.get("phase") == "rollback"]
        rollback_completed = [
            record for record in rollback_records if record.get("outcome") == "rollback_completed"
        ]
        if status == "completed" and (
            phase_outcomes != ["success"] * len(PHASES) or rollback_records
        ):
            raise UpgradeError("completed journal is incomplete")
        if status in {"failed", "safe-mode"} and rollback_completed:
            raise UpgradeError("failed journal has rollback completion")
        if status == "rolled-back" and (
            len(rollback_completed) != 1
            or not isinstance(rollback_completed[0].get("result"), dict)
            or any(
                rollback_completed[0]["result"].get(field) is not True
                for field in ("restored_verified", "runtime_validated", "backend_roundtrip_valid")
            )
        ):
            raise UpgradeError("rolled-back journal lacks rollback completion")
        if status == "rolled-back" and value.get("phase") != "rollback":
            raise UpgradeError("rolled-back journal phase is inconsistent")
        return cast(dict[str, Any], value)

    @staticmethod
    def _admit(phase: str, result: Mapping[str, Any]) -> None:
        """Execute the coordinator-native admission contract, not just booleans."""
        if phase == "preflight":
            snapshot = result.get("preflight_snapshot", result)
            if not isinstance(snapshot, Mapping):
                raise UpgradeError("preflight admission snapshot is absent")
            admit_preflight(snapshot)
        elif phase == "quiesce":
            snapshot = result.get("quiescence_snapshot", result)
            if not isinstance(snapshot, Mapping):
                raise UpgradeError("quiescence admission snapshot is absent")
            admit_quiesced(snapshot)
        elif phase == "commit":
            admitted = result.get("admitted_snapshot", result)
            current = result.get("current_snapshot", result)
            if not isinstance(admitted, Mapping) or not isinstance(current, Mapping):
                raise UpgradeError("replacement admission snapshots are absent")
            recheck_before_replacement(admitted, current)
        elif phase == "reopen":
            snapshot = result.get("reopen_snapshot", result)
            if not isinstance(snapshot, Mapping):
                raise UpgradeError("reopen admission snapshot is absent")
            admit_reopen(snapshot)

    def _bind_snapshot(self, snapshot: Mapping[str, object]) -> None:
        expected = asdict(self.context)
        for field in CONTEXT_FIELDS:
            if snapshot.get(field) != expected[field]:
                raise UpgradeError(f"admission snapshot identity mismatch: {field}")

    def apply(self, handlers: Mapping[str, Handler]) -> dict[str, Any]:
        with self._exclusive():
            return self._apply_locked(handlers)

    def _apply_locked(self, handlers: Mapping[str, Handler]) -> dict[str, Any]:  # noqa: C901
        value = self._load()
        if value["status"] == "completed":
            return value
        if value["status"] in {"failed", "safe-mode", "rolled-back"}:
            raise UpgradeError("journal requires explicit recovery before apply")
        if self.backend_adapter is None:
            raise UpgradeError("backend adapter is required for authoritative upgrade")
        records: list[dict[str, Any]] = value["records"]
        phase_records = [r.get("phase") for r in records if "phase" in r]
        if phase_records != list(dict.fromkeys(phase_records)) or phase_records != list(
            dict.fromkeys(PHASES[: len(phase_records)])
        ):
            raise UpgradeError("upgrade journal phase ordering is invalid")
        completed = {r["phase"] for r in records if r.get("outcome") == "success" and "phase" in r}
        if value["status"] == "running" and records and records[-1].get("outcome") == "started":
            raise UpgradeError("started phase requires explicit recovery")
        for phase in PHASES:
            if phase in completed:
                continue
            if phase not in handlers:
                raise UpgradeError(f"missing phase handler: {phase}")
            operation = self.operation_id
            step_id = f"{self.operation_id}.{phase}"
            record: dict[str, Any] = {
                "operation_id": operation,
                "step_id": step_id,
                "phase": phase,
                "outcome": "started",
                "context": asdict(self.context),
            }
            records.append(record)
            value["status"] = "running"
            value["phase"] = phase
            _write(self.journal, value)
            try:
                frozen_context = cast(Mapping[str, object], _freeze(asdict(self.context)))
                snapshot = self.backend_adapter.snapshot(phase, frozen_context)
                self._bind_snapshot(snapshot)
                self._admit(phase, {**snapshot})
                adapter_result = self.backend_adapter.execute(phase, frozen_context)
                result = dict(adapter_result)
                handler_result = (
                    handlers[phase](step_id, cast(Mapping[str, Any], _freeze(value))) or {}
                )
                for key in set(result).intersection(handler_result):
                    if result[key] != handler_result[key]:
                        raise UpgradeError("handler cannot override backend evidence")
                result.update(handler_result)
                self._load()
            except Exception as error:
                record.update(outcome="failed", error=type(error).__name__)
                value["status"] = "failed"
                _write(self.journal, value)
                raise UpgradeError(f"phase failed: {phase}") from error
            required = REQUIRED_EVIDENCE.get(phase, ())
            if (
                any(
                    type(result.get(field)) is not bool or result.get(field) is not True
                    for field in required
                )
                or type(result.get("mutates_authority")) is not bool
                or result.get("mutates_authority") is not PHASE_MUTATION[phase]
            ):
                record["outcome"] = "failed"
                record["error"] = "required evidence missing"
                value["status"] = "failed"
                _write(self.journal, value)
                raise UpgradeError(f"phase evidence incomplete: {phase}")
            if result.get("backend") != self.context.backend:
                record.update(outcome="failed", error="backend identity mismatch")
                value["status"] = "failed"
                _write(self.journal, value)
                raise UpgradeError(f"phase backend mismatch: {phase}")
            try:
                self._admit(phase, result)
            except Exception as error:
                record.update(outcome="failed", error=type(error).__name__)
                value["status"] = "failed"
                _write(self.journal, value)
                raise UpgradeError(f"phase admission denied: {phase}") from error
            if (
                phase in {"quiesce", "backup", "stage", "commit", "validate", "reopen"}
                and result.get("fencing_token") != self.context.fencing_token
            ):
                record.update(outcome="failed", error="fencing token mismatch")
                value["status"] = "failed"
                _write(self.journal, value)
                raise UpgradeError(f"phase fencing mismatch: {phase}")
            if result.get("ambiguous") is True:
                record.update(outcome="ambiguous", error="external outcome is ambiguous")
                value["status"] = "safe-mode"
                _write(self.journal, value)
                raise UpgradeError(f"phase outcome is ambiguous: {phase}")
            record["outcome"] = "success"
            record["result"] = dict(result)
            _write(self.journal, value)
        value["status"] = "completed"
        _write(self.journal, value)
        return value

    def rollback(self, handler: Handler) -> dict[str, Any]:
        with self._exclusive():
            if self.backend_adapter is None:
                raise UpgradeError("backend adapter is required for rollback")
            snapshot = self.backend_adapter.snapshot(
                "rollback", cast(Mapping[str, object], _freeze(asdict(self.context)))
            )
            if snapshot.get("rollback_context_verified") is not True:
                raise UpgradeError("backend did not verify rollback context")
            supplied = {field: snapshot.get(field) for field in CONTEXT_FIELDS}
            _validate_context(supplied, self.operation_id)
            if supplied["target"] != "rollback":
                raise UpgradeError("rollback context target is invalid")
            for field in CONTEXT_FIELDS:
                if (
                    field not in {"target", "barrier_identity_digest", "envelope_digest"}
                    and supplied[field] != asdict(self.context)[field]
                ):
                    raise UpgradeError(f"rollback context mismatch: {field}")
            verifier = getattr(self.backend_adapter, "verify_rollback_context", None)
            if not callable(verifier) or verifier(supplied) is not True:
                raise UpgradeError("rollback context is not verified by authority")
            self._verified_rollback_context = supplied
            return self._rollback_locked(handler)

    def _rollback_locked(self, handler: Handler) -> dict[str, Any]:
        value = self._load()
        if self.backend_adapter is None:
            raise UpgradeError("backend adapter is required for rollback")
        if value["status"] not in {"failed", "running", "safe-mode"}:
            raise UpgradeError("rollback requires failed, running, or safe-mode operation")
        if any(record.get("phase") == "rollback" for record in value["records"]):
            raise UpgradeError("rollback outcome requires explicit reconciliation")
        operation = self.operation_id
        step_id = f"{self.operation_id}.rollback"
        record: dict[str, Any] = {
            "operation_id": operation,
            "step_id": step_id,
            "phase": "rollback",
            "outcome": "started",
            "context": self._verified_rollback_context,
        }
        value["records"].append(record)
        _write(self.journal, value)
        try:
            adapter_result = dict(
                self.backend_adapter.execute(
                    "rollback", cast(Mapping[str, object], _freeze(self._verified_rollback_context))
                )
            )
            required = ("restored_verified", "runtime_validated", "backend_roundtrip_valid")
            if any(
                type(adapter_result.get(field)) is not bool or adapter_result.get(field) is not True
                for field in required
            ):
                raise UpgradeError("backend did not verify known-good runtime")
            result = dict(adapter_result)
            handler_result = handler(step_id, cast(Mapping[str, Any], _freeze(value))) or {}
            for key in set(result).intersection(handler_result):
                if result[key] != handler_result[key]:
                    raise UpgradeError("handler cannot override backend evidence")
            if set(handler_result).intersection(required):
                raise UpgradeError("handler cannot provide backend rollback evidence")
            result.update(handler_result)
            record["outcome"] = "rollback_completed"
            record["result"] = result
            value["status"] = "rolled-back"
            value["phase"] = "rollback"
        except Exception as error:
            record.update(outcome="ambiguous", error=type(error).__name__)
            value["status"] = "safe-mode"
            _write(self.journal, value)
            raise UpgradeError("rollback ambiguous; safe mode required") from error
        _write(self.journal, value)
        return value
