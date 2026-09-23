# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Backend-neutral single-use contract for isolated authority effects."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

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
        except Exception as error:
            # A failed or ambiguous effect is never retried through this token.
            if isinstance(error, (AuthorityMutationAmbiguousError, TimeoutError)):
                raise AuthorityMutationAmbiguousError(
                    "authority mutation outcome is ambiguous; recovery is required"
                ) from error
            raise
        def field(name: str) -> object:
            if isinstance(result, Mapping):
                return result.get(name)
            return getattr(result, name, None)

        if (
            field("operation_id") != self._admission.operation_id
            or field("fencing_token") != self._admission.fencing_token
            or field("mutates_authority") is not True
        ):
            raise AuthorityMutationError("authority mutation result identity mismatch")
        return MutationReceipt(
            backend=backend,
            operation_id=self._admission.operation_id,
            fencing_token=self._admission.fencing_token,
            mutates_authority=True,
        )
