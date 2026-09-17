# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Public-safe, revision-bound safe-exit journal transitions."""

from __future__ import annotations

import re
from dataclasses import dataclass

PUBLIC_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class ExitError(ValueError):
    """A malformed, stale, duplicate, incomplete, or unsafe exit transition."""


@dataclass(frozen=True, slots=True)
class FutureDiscussion:
    """A future request represented without retaining private transcript text."""

    request_ref: str
    request_digest: str
    ar_ref: str | None = None

    def __post_init__(self) -> None:
        _ref(self.request_ref, "future request ref")
        _digest(self.request_digest, "future request digest")
        if self.ar_ref is not None:
            _ar_ref(self.ar_ref)


@dataclass(frozen=True, slots=True)
class ExitSnapshot:
    """All durable exit inputs, represented only by public refs/digests."""

    snapshot_ref: str
    snapshot_digest: str
    proposal_refs: tuple[str, ...]
    selection_refs: tuple[str, ...]
    custom_solution_refs: tuple[str, ...]
    unresolved_refs: tuple[str, ...]
    future_requests: tuple[FutureDiscussion, ...]

    def __post_init__(self) -> None:
        _ref(self.snapshot_ref, "snapshot ref")
        _digest(self.snapshot_digest, "snapshot digest")
        for refs, label in (
            (self.proposal_refs, "proposal"),
            (self.selection_refs, "selection"),
            (self.custom_solution_refs, "custom solution"),
            (self.unresolved_refs, "unresolved"),
        ):
            for value in refs:
                _ref(value, f"{label} ref")


@dataclass(frozen=True, slots=True)
class ExitEvent:
    event_id: str
    task_id: str
    task_revision: int
    action: str
    snapshot: ExitSnapshot | None = None
    checkpoint_digest: str | None = None

    def __post_init__(self) -> None:
        _ref(self.event_id, "event id")
        if not re.fullmatch(r"AR-\d{4}", self.task_id):
            raise ExitError("task_id is invalid")
        if type(self.task_revision) is not int or self.task_revision < 1:
            raise ExitError("task_revision must be positive")
        if self.action not in {"checkpoint", "map_future", "commit_exit", "resume"}:
            raise ExitError("exit action is invalid")
        if self.checkpoint_digest is not None:
            _digest(self.checkpoint_digest, "checkpoint digest")


class SafeExitJournal:
    """Atomic-in-memory journal model for checkpoint, recovery, and exit."""

    def __init__(self, task_id: str, task_revision: int) -> None:
        if not re.fullmatch(r"AR-\d{4}", task_id) or task_revision < 1:
            raise ExitError("journal identity is invalid")
        self.task_id = task_id
        self.task_revision = task_revision
        self.snapshot: ExitSnapshot | None = None
        self.pending = False
        self.committed = False
        self._event_ids: set[str] = set()

    def apply(self, event: ExitEvent) -> None:  # noqa: C901
        if event.event_id in self._event_ids:
            raise ExitError("duplicate exit event")
        if event.task_id != self.task_id or event.task_revision != self.task_revision:
            raise ExitError("stale exit revision")
        if self.committed:
            raise ExitError("exit is already committed")
        if event.action == "checkpoint":
            if event.snapshot is None or self.pending:
                raise ExitError("checkpoint is missing or already pending")
            self.snapshot = event.snapshot
            self.pending = True
        elif event.action == "resume":
            if not self.pending or self.snapshot is None:
                raise ExitError("resume requires an interrupted checkpoint")
            if event.checkpoint_digest != self.snapshot.snapshot_digest:
                raise ExitError("resume checkpoint does not match")
        elif event.action == "map_future":
            if not self.pending or self.snapshot is None:
                raise ExitError("future mapping requires a checkpoint")
            if event.snapshot is None:
                raise ExitError("future mapping is missing request")
            if len(event.snapshot.future_requests) != 1:
                raise ExitError("future mapping requires one request")
            request = event.snapshot.future_requests[0]
            if request.ar_ref is None:
                raise ExitError("future request has no AR mapping")
            self.snapshot = _replace_future(self.snapshot, request)
        else:
            if not self.pending or self.snapshot is None:
                raise ExitError("exit requires a checkpoint")
            if any(request.ar_ref is None for request in self.snapshot.future_requests):
                raise ExitError("future discussion request is unmapped")
            self.pending = False
            self.committed = True
        self._event_ids.add(event.event_id)
        self.task_revision += 1


def _replace_future(snapshot: ExitSnapshot, replacement: FutureDiscussion) -> ExitSnapshot:
    requests = tuple(
        replacement if item.request_ref == replacement.request_ref else item
        for item in snapshot.future_requests
    )
    if requests == snapshot.future_requests:
        raise ExitError("future request is not in checkpoint")
    return ExitSnapshot(
        snapshot.snapshot_ref,
        snapshot.snapshot_digest,
        snapshot.proposal_refs,
        snapshot.selection_refs,
        snapshot.custom_solution_refs,
        snapshot.unresolved_refs,
        requests,
    )


def _ref(value: str, label: str) -> None:
    if not isinstance(value, str) or not PUBLIC_REF.fullmatch(value):
        raise ExitError(f"{label} must be public-safe")
    if value.startswith("/") or ".." in value or "//" in value:
        raise ExitError(f"{label} must be public-safe")


def _digest(value: str, label: str) -> None:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ExitError(f"{label} must be a sha256 digest")


def _ar_ref(value: str) -> None:
    if not re.fullmatch(r"AR-\d{4}", value):
        raise ExitError("future AR mapping is invalid")
