# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Durable fail-closed phase journal for the upgrade engine."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

PHASES = ("discover", "preflight", "quiesce", "backup", "stage", "commit", "validate", "reopen")


class UpgradeError(RuntimeError):
    """An upgrade cannot safely advance."""


Handler = Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None]


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
        temporary.unlink(missing_ok=True)
        raise UpgradeError("durable upgrade journal write failed") from error


class UpgradeEngine:
    """Execute exactly eight ordered phases with durable outcomes."""

    def __init__(self, operation_id: str, journal: Path) -> None:
        if not operation_id or ":" in operation_id:
            raise UpgradeError("invalid operation identity")
        self.operation_id = operation_id
        self.journal = journal

    def check(self) -> dict[str, object]:
        return {"operation_id": self.operation_id, "phases": list(PHASES), "checked": True}

    def plan(self) -> dict[str, Any]:
        if self.journal.exists():
            raise UpgradeError("operation already planned")
        value: dict[str, Any] = {
            "operation_id": self.operation_id,
            "status": "planned",
            "phase": None,
            "records": [],
        }
        _write(self.journal, value)
        return value

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.journal.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise UpgradeError("upgrade journal is unreadable") from error
        if value.get("operation_id") != self.operation_id or not isinstance(
            value.get("records"), list
        ):
            raise UpgradeError("upgrade journal identity or records are invalid")
        return value

    def apply(self, handlers: Mapping[str, Handler]) -> dict[str, Any]:
        value = self._load()
        if value["status"] == "completed":
            return value
        records: list[dict[str, Any]] = value["records"]
        completed = {r["phase"] for r in records if r.get("outcome") == "success" and "phase" in r}
        for phase in PHASES:
            if phase in completed:
                continue
            if phase not in handlers:
                raise UpgradeError(f"missing phase handler: {phase}")
            operation = f"{self.operation_id}:{phase}"
            record = {"operation_id": operation, "phase": phase, "outcome": "started"}
            records.append(record)
            value.update(status="running", phase=phase)
            _write(self.journal, value)
            try:
                result = handlers[phase](operation, value) or {}
            except Exception as error:
                record.update(outcome="failed", error=type(error).__name__)
                value["status"] = "failed"
                _write(self.journal, value)
                raise UpgradeError(f"phase failed: {phase}") from error
            record.update(outcome="success", result=dict(result))
            _write(self.journal, value)
        value["status"] = "completed"
        _write(self.journal, value)
        return value

    def rollback(self, handler: Handler) -> dict[str, Any]:
        value = self._load()
        if value["status"] not in {"failed", "running"}:
            raise UpgradeError("rollback requires failed or running operation")
        operation = f"{self.operation_id}:rollback"
        record = {"operation_id": operation, "outcome": "started"}
        value["records"].append(record)
        try:
            record.update(outcome="success", result=dict(handler(operation, value) or {}))
            value["status"] = "rolled-back"
        except Exception as error:
            record.update(outcome="ambiguous", error=type(error).__name__)
            value["status"] = "safe-mode"
            _write(self.journal, value)
            raise UpgradeError("rollback ambiguous; safe mode required") from error
        _write(self.journal, value)
        return value
