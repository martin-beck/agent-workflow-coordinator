# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Public, typed evidence contract for the three-project oracle workflow.

Coordinator owns the lifecycle decision. AWG supplies guidance artifacts and
AWQ supplies quality/formal evidence; neither downstream projection can
authorize continuation by itself.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

PUBLIC_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
TASK_ID = re.compile(r"^AR-\d{4}$")
PROJECTS = frozenset({"coordinator", "guidance", "quality"})


class IntegrationError(ValueError):
    """Malformed, incomplete, stale, or authority-confused integration evidence."""


class EvidenceKind(StrEnum):
    GUIDANCE = "guidance"
    QUALITY = "quality"
    FORMAL = "formal"


def _ref(value: object, label: str) -> str:
    if not isinstance(value, str) or not PUBLIC_REF.fullmatch(value):
        raise IntegrationError(f"{label} must be a public-safe reference")
    if value.startswith("/") or ".." in value or "//" in value:
        raise IntegrationError(f"{label} must be a public-safe reference")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise IntegrationError(f"{label} must be a sha256 digest")
    return value


@dataclass(frozen=True, slots=True)
class Evidence:
    """One public projection from AWG or AWQ."""

    project: str
    kind: EvidenceKind
    ref: str
    digest: str
    task_revision: int

    def __post_init__(self) -> None:
        if self.project not in PROJECTS - {"coordinator"}:
            raise IntegrationError("evidence project must be guidance or quality")
        try:
            object.__setattr__(self, "kind", EvidenceKind(str(self.kind)))
        except ValueError as error:
            raise IntegrationError("evidence kind is invalid") from error
        _ref(self.ref, "evidence ref")
        _digest(self.digest, "evidence digest")
        if type(self.task_revision) is not int or self.task_revision < 1:
            raise IntegrationError("evidence task revision must be positive")
        if self.project == "guidance" and self.kind is not EvidenceKind.GUIDANCE:
            raise IntegrationError("guidance evidence kind is invalid")
        if self.project == "quality" and self.kind not in {
            EvidenceKind.QUALITY,
            EvidenceKind.FORMAL,
        }:
            raise IntegrationError("quality evidence kind is invalid")

    def as_record(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "kind": self.kind.value,
            "ref": self.ref,
            "digest": self.digest,
            "task_revision": self.task_revision,
        }


@dataclass(frozen=True, slots=True)
class IntegrationContract:
    """A complete, non-authorizing cross-project workflow observation."""

    task_id: str
    task_revision: int
    guidance: Evidence
    quality: Evidence
    formal: Evidence
    coordinator_event_ref: str
    decision_authority: str = "coordinator"

    def __post_init__(self) -> None:
        if not TASK_ID.fullmatch(self.task_id):
            raise IntegrationError("integration task id is invalid")
        if type(self.task_revision) is not int or self.task_revision < 1:
            raise IntegrationError("integration task revision must be positive")
        if self.decision_authority != "coordinator":
            raise IntegrationError("only Coordinator may authorize continuation")
        _ref(self.coordinator_event_ref, "coordinator event ref")
        for evidence in (self.guidance, self.quality, self.formal):
            if evidence.task_revision != self.task_revision:
                raise IntegrationError("evidence revision does not match integration revision")
        if self.guidance.project != "guidance":
            raise IntegrationError("guidance projection is missing")
        if self.quality.project != "quality" or self.formal.project != "quality":
            raise IntegrationError("quality projections are missing")

    def as_record(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_revision": self.task_revision,
            "guidance": self.guidance.as_record(),
            "quality": self.quality.as_record(),
            "formal": self.formal.as_record(),
            "coordinator_event_ref": self.coordinator_event_ref,
            "decision_authority": self.decision_authority,
        }

    @property
    def digest(self) -> str:
        payload = json.dumps(self.as_record(), sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(payload).hexdigest()


def integration_errors(value: object) -> list[str]:
    """Validate a serialized integration contract without retaining transcripts."""
    if not isinstance(value, dict):
        return ["integration contract must be an object"]
    required = {
        "task_id",
        "task_revision",
        "guidance",
        "quality",
        "formal",
        "coordinator_event_ref",
        "decision_authority",
    }
    if set(value) != required:
        return ["integration contract fields are incomplete or unknown"]
    try:
        evidence = []
        for name in ("guidance", "quality", "formal"):
            item = value[name]
            if not isinstance(item, dict) or set(item) != {
                "project",
                "kind",
                "ref",
                "digest",
                "task_revision",
            }:
                raise IntegrationError(f"{name} evidence is incomplete")
            evidence.append(Evidence(**item))
        IntegrationContract(
            task_id=value["task_id"],
            task_revision=value["task_revision"],
            guidance=evidence[0],
            quality=evidence[1],
            formal=evidence[2],
            coordinator_event_ref=value["coordinator_event_ref"],
            decision_authority=value["decision_authority"],
        )
    except (IntegrationError, TypeError, ValueError) as error:
        return [f"integration contract invalid: {error}"]
    return []
