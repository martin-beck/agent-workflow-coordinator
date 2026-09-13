# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Durable fail-closed phase journal for the upgrade engine."""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

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


class UpgradeError(RuntimeError):
    """An upgrade cannot safely advance."""


Handler = Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None]
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
CONTEXT_FIELDS = (
    "operation_id",
    "state_revision",
    "fencing_token",
    "fencing_owner",
    "backend",
    "authority_revision",
    "target",
)


@dataclass(frozen=True)
class PhaseContext:
    """Immutable identity and backend binding for one upgrade operation."""

    operation_id: str
    state_revision: int
    fencing_token: str
    fencing_owner: str
    backend: str
    authority_revision: str
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


class UpgradeEngine:
    """Execute exactly eight ordered phases with durable outcomes."""

    def __init__(
        self,
        operation_id: str,
        journal: Path,
        context: Mapping[str, object],
        lock_path: Path | None = None,
    ) -> None:
        if not operation_id or ":" in operation_id:
            raise UpgradeError("invalid operation identity")
        self.operation_id = operation_id
        self.journal = journal
        self.lock_path = lock_path or journal.parent / ".upgrade-engine.lock"
        supplied = dict(context)
        if set(supplied) != set(CONTEXT_FIELDS) or supplied.get("operation_id") != operation_id:
            raise UpgradeError("complete bound phase context is required")
        if (
            not isinstance(supplied["state_revision"], int)
            or isinstance(supplied["state_revision"], bool)
            or supplied["state_revision"] < 1
        ):
            raise UpgradeError("invalid state revision")
        if any(
            not isinstance(supplied[field], str) or not supplied[field]
            for field in CONTEXT_FIELDS
            if field != "state_revision"
        ):
            raise UpgradeError("invalid phase context identity")
        if supplied["backend"] not in {"git", "sqlite"} or supplied["target"] not in {
            "new",
            "rollback",
        }:
            raise UpgradeError("unsupported backend or target")
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
        for record in records:
            if not isinstance(record, dict):
                raise UpgradeError("upgrade journal record is invalid")
            phase = record.get("phase")
            if phase == "rollback":
                if record.get("operation_id") != f"{self.operation_id}:rollback":
                    raise UpgradeError("upgrade journal rollback identity is invalid")
                if record.get("outcome") not in {"started", "success", "ambiguous"}:
                    raise UpgradeError("upgrade journal rollback outcome is invalid")
                continue
            if phase is not None:
                if (
                    expected >= len(PHASES)
                    or phase != PHASES[expected]
                    or record.get("operation_id") != f"{self.operation_id}:{phase}"
                ):
                    raise UpgradeError("upgrade journal phase identity is invalid")
                if record.get("outcome") not in {"started", "success", "failed", "ambiguous"}:
                    raise UpgradeError("upgrade journal outcome is invalid")
                if set(record) - {"operation_id", "phase", "outcome", "result", "error"}:
                    raise UpgradeError("upgrade journal record fields are invalid")
                outcome = record["outcome"]
                if outcome == "success" and not isinstance(record.get("result"), dict):
                    raise UpgradeError("successful phase lacks result evidence")
                if outcome in {"failed", "ambiguous"} and not isinstance(record.get("error"), str):
                    raise UpgradeError("failed phase lacks error evidence")
                if outcome == "started" and set(record) != {"operation_id", "phase", "outcome"}:
                    raise UpgradeError("started phase has terminal evidence")
                phase_outcomes.append(cast(str, record["outcome"]))
                expected += 1
            elif record.get("operation_id") != f"{self.operation_id}:rollback":
                raise UpgradeError("upgrade journal auxiliary identity is invalid")
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
        if status == "completed" and phase_outcomes != ["success"] * len(PHASES):
            raise UpgradeError("completed journal is incomplete")
        return cast(dict[str, Any], value)

    @staticmethod
    def _admit(phase: str, result: Mapping[str, Any]) -> None:
        """Execute the coordinator-native admission contract, not just booleans."""
        if phase == "preflight":
            snapshot = result.get("preflight_snapshot")
            if not isinstance(snapshot, Mapping):
                raise UpgradeError("preflight admission snapshot is absent")
            admit_preflight(snapshot)
        elif phase == "quiesce":
            snapshot = result.get("quiescence_snapshot")
            if not isinstance(snapshot, Mapping):
                raise UpgradeError("quiescence admission snapshot is absent")
            admit_quiesced(snapshot)
        elif phase == "commit":
            admitted = result.get("admitted_snapshot")
            current = result.get("current_snapshot")
            if not isinstance(admitted, Mapping) or not isinstance(current, Mapping):
                raise UpgradeError("replacement admission snapshots are absent")
            recheck_before_replacement(admitted, current)
        elif phase == "reopen":
            snapshot = result.get("reopen_snapshot")
            if not isinstance(snapshot, Mapping):
                raise UpgradeError("reopen admission snapshot is absent")
            admit_reopen(snapshot)

    def apply(self, handlers: Mapping[str, Handler]) -> dict[str, Any]:
        with self._exclusive():
            return self._apply_locked(handlers)

    def _apply_locked(self, handlers: Mapping[str, Handler]) -> dict[str, Any]:  # noqa: C901
        value = self._load()
        if value["status"] == "completed":
            return value
        if value["status"] in {"failed", "safe-mode", "rolled-back"}:
            raise UpgradeError("journal requires explicit recovery before apply")
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
            operation = f"{self.operation_id}:{phase}"
            record: dict[str, Any] = {
                "operation_id": operation,
                "phase": phase,
                "outcome": "started",
            }
            records.append(record)
            value["status"] = "running"
            value["phase"] = phase
            _write(self.journal, value)
            try:
                result = handlers[phase](operation, cast(Mapping[str, Any], _freeze(value))) or {}
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
            return self._rollback_locked(handler)

    def _rollback_locked(self, handler: Handler) -> dict[str, Any]:
        value = self._load()
        if value["status"] not in {"failed", "running", "safe-mode"}:
            raise UpgradeError("rollback requires failed, running, or safe-mode operation")
        if any(record.get("phase") == "rollback" for record in value["records"]):
            raise UpgradeError("rollback outcome requires explicit reconciliation")
        operation = f"{self.operation_id}:rollback"
        record: dict[str, Any] = {
            "operation_id": operation,
            "phase": "rollback",
            "outcome": "started",
        }
        value["records"].append(record)
        _write(self.journal, value)
        try:
            result = dict(handler(operation, cast(Mapping[str, Any], _freeze(value))) or {})
            required = ("restored_verified", "runtime_validated", "backend_roundtrip_valid")
            if any(
                type(result.get(field)) is not bool or result.get(field) is not True
                for field in required
            ):
                record["outcome"] = "ambiguous"
                value["status"] = "safe-mode"
                _write(self.journal, value)
                raise UpgradeError("rollback did not verify known-good runtime")
            record["outcome"] = "success"
            record["result"] = result
            value["status"] = "rolled-back"
        except Exception as error:
            record.update(outcome="ambiguous", error=type(error).__name__)
            value["status"] = "safe-mode"
            _write(self.journal, value)
            raise UpgradeError("rollback ambiguous; safe mode required") from error
        _write(self.journal, value)
        return value
