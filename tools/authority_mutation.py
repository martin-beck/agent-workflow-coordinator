# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Backend-neutral single-use contract for isolated authority effects."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from tools.authority_neutral_commit import CommitAdmissionBundle


class AuthorityMutationError(RuntimeError):
    """An authority effect was rejected or already consumed."""


class AuthorityMutationAmbiguousError(AuthorityMutationError):
    """An effect had an uncertain outcome and cannot be retried."""


@dataclass(frozen=True)
class MutationReceipt:
    backend: str
    operation_id: str
    fencing_token: str
    mutates_authority: bool


class AuthorityEffectJournal(Protocol):
    """Durable pre-effect journal used by an isolated mutation capability."""

    def prepare_authority_effect(
        self,
        expected_revision: int,
        operation_id: str,
        backend: str,
        target: str,
        *,
        expected_fencing_token: str | None = None,
        expected_barrier_id: str | None = None,
        expected_artifact_identity: str | None = None,
        expected_manifest_identity: str | None = None,
        expected_selector_identity: str | None = None,
        expected_runtime_identity: str | None = None,
    ) -> object: ...

    def finish_authority_effect(self, intent: Any, outcome: str) -> Any: ...


class BoundAuthorityMutation:
    """Bind one backend effect to one immutable admission and consume once."""

    def __init__(self, admission: CommitAdmissionBundle) -> None:
        self._admission = admission
        self._consumed = False

    def execute(self, backend: str, effect: Callable[[], object]) -> MutationReceipt:
        if self._consumed:
            raise AuthorityMutationError("authority mutation capability already consumed")
        if backend != self._admission.backend:
            raise AuthorityMutationError("authority mutation backend identity mismatch")
        if not callable(effect):
            raise AuthorityMutationError("authority mutation effect is invalid")
        self._consumed = True
        try:
            result = effect()
        except BaseException as error:
            # Any effect that does not return a verified result, including a
            # termination exception, has an uncertain authority outcome.
            raise AuthorityMutationAmbiguousError(
                "authority mutation outcome is ambiguous; recovery is required"
            ) from error

        def field(name: str) -> object:
            if isinstance(result, Mapping):
                return result.get(name)
            return getattr(result, name, None)

        expected_identity = {
            "backend": backend,
            "target": self._admission.target,
            "operation_id": self._admission.operation_id,
            "state_revision": self._admission.state_revision,
            "barrier_id": self._admission.barrier_id,
            "artifact_identity": self._admission.artifact_identity,
            "manifest_identity": self._admission.manifest_identity,
            "selector_identity": self._admission.selector_identity,
            "runtime_identity": self._admission.runtime_identity,
            "fencing_token": self._admission.fencing_token,
        }
        if (
            any(field(name) != value for name, value in expected_identity.items())
            or field("mutates_authority") is not True
        ):
            raise AuthorityMutationError("authority mutation result identity mismatch")
        return MutationReceipt(
            backend=backend,
            operation_id=self._admission.operation_id,
            fencing_token=self._admission.fencing_token,
            mutates_authority=True,
        )


class DurableBoundAuthorityMutation:
    """Add durable process-death fencing around one isolated authority effect.

    The journal is prepared before the effect and completed only after the
    single-use capability returns a verified receipt.  A worker killed between
    those points leaves a prepared intent for the control-store recovery path;
    it never turns an unknown outcome into a retryable success.  This adapter
    is deliberately not connected to public upgrade dispatch.
    """

    def __init__(
        self,
        admission: CommitAdmissionBundle,
        journal: AuthorityEffectJournal,
        *,
        session_revision: int,
    ) -> None:
        if type(session_revision) is not int or session_revision < 1:
            raise AuthorityMutationError("authority mutation session revision is invalid")
        if admission.state_revision != session_revision:
            raise AuthorityMutationError("authority mutation admission/session revision mismatch")
        self._admission = admission
        self._journal = journal
        self._session_revision = session_revision
        self._capability = BoundAuthorityMutation(admission)

    def execute(self, effect: Callable[[], object]) -> MutationReceipt:
        intent = self._journal.prepare_authority_effect(
            self._session_revision,
            self._admission.operation_id,
            self._admission.backend,
            self._admission.target,
            expected_fencing_token=self._admission.fencing_token,
            expected_barrier_id=self._admission.barrier_id,
            expected_artifact_identity=self._admission.artifact_identity,
            expected_manifest_identity=self._admission.manifest_identity,
            expected_selector_identity=self._admission.selector_identity,
            expected_runtime_identity=self._admission.runtime_identity,
        )
        try:
            receipt = self._capability.execute(self._admission.backend, effect)
        except BaseException:
            try:
                self._journal.finish_authority_effect(intent, "ambiguous")
            except BaseException as journal_error:
                raise AuthorityMutationAmbiguousError(
                    "authority mutation recovery journal is ambiguous"
                ) from journal_error
            raise
        try:
            self._journal.finish_authority_effect(intent, "committed")
        except BaseException as error:
            raise AuthorityMutationAmbiguousError(
                "authority mutation outcome publication is ambiguous"
            ) from error
        return receipt
