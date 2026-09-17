# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Revision-bound, public-safe planning and design artifact bindings.

Coordinator stores only typed references and digests.  Artifact contents remain
owned by the project that publishes them; this module validates the durable
identity and reopen boundary without reading or copying those contents.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

ARTIFACT_TYPES = (
    "work_plan",
    "design_document",
    "dependency_graph",
    "ar_manifest",
    "formal_specification",
)
PUBLIC_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
TASK_ID = re.compile(r"^AR-\d{4}$")


class ArtifactType(StrEnum):
    WORK_PLAN = "work_plan"
    DESIGN_DOCUMENT = "design_document"
    DEPENDENCY_GRAPH = "dependency_graph"
    AR_MANIFEST = "ar_manifest"
    FORMAL_SPECIFICATION = "formal_specification"


class ArtifactBindingError(ValueError):
    """A malformed, stale, incomplete, or unsafe artifact binding."""


def _public_ref(value: object, label: str) -> str:
    if not isinstance(value, str) or not PUBLIC_REF.fullmatch(value):
        raise ArtifactBindingError(f"{label} must be a public-safe reference")
    if value.startswith("/") or ".." in value or "//" in value:
        raise ArtifactBindingError(f"{label} must be a public-safe reference")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ArtifactBindingError(f"{label} must be a sha256 digest")
    return value


@dataclass(frozen=True, slots=True)
class ArtifactSnapshot:
    """One immutable public reference to a versioned project artifact."""

    artifact_type: ArtifactType
    ref: str
    digest: str
    scope: str
    task_revision: int
    version: int
    predecessors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            artifact_type = ArtifactType(str(self.artifact_type))
        except ValueError as error:
            raise ArtifactBindingError("artifact type is invalid") from error
        object.__setattr__(self, "artifact_type", artifact_type)
        _public_ref(self.ref, "artifact ref")
        _digest(self.digest, "artifact digest")
        _public_ref(self.scope, "artifact scope")
        if type(self.task_revision) is not int or self.task_revision < 1:
            raise ArtifactBindingError("artifact task revision must be positive")
        if type(self.version) is not int or self.version < 1:
            raise ArtifactBindingError("artifact version must be positive")
        if len(set(self.predecessors)) != len(self.predecessors):
            raise ArtifactBindingError("artifact predecessors must be unique")
        for predecessor in self.predecessors:
            _public_ref(predecessor, "artifact predecessor")

    def as_record(self) -> dict[str, Any]:
        return {
            "artifact_type": self.artifact_type.value,
            "ref": self.ref,
            "digest": self.digest,
            "scope": self.scope,
            "task_revision": self.task_revision,
            "version": self.version,
            "predecessors": list(self.predecessors),
        }

    @classmethod
    def from_record(cls, value: Mapping[str, Any]) -> ArtifactSnapshot:
        required = {
            "artifact_type",
            "ref",
            "digest",
            "scope",
            "task_revision",
            "version",
            "predecessors",
        }
        if set(value) != required:
            raise ArtifactBindingError("artifact snapshot fields are incomplete or unknown")
        predecessors = value["predecessors"]
        if not isinstance(predecessors, list) or any(
            not isinstance(item, str) for item in predecessors
        ):
            raise ArtifactBindingError("artifact predecessors must be a list of references")
        try:
            return cls(
                artifact_type=ArtifactType(str(value["artifact_type"])),
                ref=str(value["ref"]),
                digest=str(value["digest"]),
                scope=str(value["scope"]),
                task_revision=value["task_revision"],
                version=value["version"],
                predecessors=tuple(predecessors),
            )
        except (TypeError, ValueError) as error:
            raise ArtifactBindingError(str(error)) from error


@dataclass(frozen=True, slots=True)
class ArtifactBinding:
    """A before/after artifact set attached to exactly one task revision."""

    task_id: str
    task_revision: int
    before: tuple[ArtifactSnapshot, ...]
    after: tuple[ArtifactSnapshot, ...]
    action: str = "record"
    reopened_dependents: tuple[str, ...] = ()

    def __post_init__(self) -> None:  # noqa: C901
        if not TASK_ID.fullmatch(self.task_id):
            raise ArtifactBindingError("binding task id is invalid")
        if type(self.task_revision) is not int or self.task_revision < 1:
            raise ArtifactBindingError("binding task revision must be positive")
        if self.action not in {"record", "reopen"}:
            raise ArtifactBindingError("binding action is invalid")
        for label, snapshots in (("before", self.before), ("after", self.after)):
            if len(snapshots) != len(ARTIFACT_TYPES):
                raise ArtifactBindingError(f"binding {label} artifacts are incomplete")
            kinds = [item.artifact_type.value for item in snapshots]
            if set(kinds) != set(ARTIFACT_TYPES):
                raise ArtifactBindingError(f"binding {label} artifacts must contain every type")
            if len(kinds) != len(set(kinds)):
                raise ArtifactBindingError(f"binding {label} artifacts contain duplicate types")
            if any(item.task_revision != self.task_revision for item in snapshots):
                raise ArtifactBindingError(f"binding {label} revision does not match task revision")
        if len(set(self.reopened_dependents)) != len(self.reopened_dependents):
            raise ArtifactBindingError("reopened dependents must be unique")
        for dependent in self.reopened_dependents:
            if not TASK_ID.fullmatch(dependent):
                raise ArtifactBindingError("reopened dependent task id is invalid")
        changed = {item.artifact_type for item in self.before} != {
            item.artifact_type for item in self.after
        }
        changed = changed or any(
            left.digest != right.digest or left.version != right.version
            for left, right in zip(
                sorted(self.before, key=lambda item: item.artifact_type.value),
                sorted(self.after, key=lambda item: item.artifact_type.value),
                strict=True,
            )
        )
        if changed and self.action != "reopen":
            raise ArtifactBindingError("changed artifacts require explicit reopen")
        if self.action == "reopen" and not self.reopened_dependents:
            raise ArtifactBindingError("reopen requires dependent task ids")

    def as_record(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_revision": self.task_revision,
            "before": [item.as_record() for item in self.before],
            "after": [item.as_record() for item in self.after],
            "action": self.action,
            "reopened_dependents": list(self.reopened_dependents),
        }

    @property
    def digest(self) -> str:
        encoded = json.dumps(self.as_record(), sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


def binding_errors(value: object) -> list[str]:
    """Return validation errors for an optional serialized binding."""
    if value is None:
        return []
    if not isinstance(value, dict):
        return ["artifact_binding must be an object"]
    required = {"task_id", "task_revision", "before", "after", "action", "reopened_dependents"}
    if set(value) != required:
        return ["artifact_binding fields are incomplete or unknown"]
    try:
        before = tuple(ArtifactSnapshot.from_record(item) for item in value["before"])
        after = tuple(ArtifactSnapshot.from_record(item) for item in value["after"])
        ArtifactBinding(
            task_id=str(value["task_id"]),
            task_revision=value["task_revision"],
            before=before,
            after=after,
            action=str(value["action"]),
            reopened_dependents=tuple(value["reopened_dependents"]),
        )
    except (ArtifactBindingError, TypeError, ValueError) as error:
        return [f"artifact_binding invalid: {error}"]
    return []
