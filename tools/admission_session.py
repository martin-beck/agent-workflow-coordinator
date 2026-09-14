# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Uncalled caller-owned session boundary for admitted coordinator writes.

The coordinator does not currently have a production caller that can prove
the durable barrier, authority reread, and lock order together. This module
therefore provides only the narrow integration seam for that future caller.
It composes the already-reviewed control-store and selector adapters without
changing any legacy constructor or enabling an upgrade mutation path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tools.admission_lease import AdmissionLease, AdmissionLeaseError, AdmissionRecheck
from tools.admitted_control_store import AdmittedControlStore, OrderedAdmissionScope, admitted_cas


@dataclass(frozen=True, slots=True)
class AdmissionSession:
    """Caller-owned evidence and scope for one admitted mutation session.

    The session does not acquire or manufacture a barrier. The caller must
    supply the immutable lease, its exact recheck, and the concrete ordered
    scope. Each operation delegates to a fail-closed adapter and acquires the
    scope only for that operation; a failed operation therefore cannot leave a
    session-wide lock held.
    """

    lease: AdmissionLease
    recheck: AdmissionRecheck
    scope: OrderedAdmissionScope

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if not isinstance(self.lease, AdmissionLease):
            raise AdmissionLeaseError("admission session lease is required")
        if not isinstance(self.recheck, AdmissionRecheck):
            raise AdmissionLeaseError("admission session recheck is required")
        if self.recheck.lease != self.lease:
            raise AdmissionLeaseError("admission session recheck does not match lease")
        if not isinstance(self.scope, OrderedAdmissionScope):
            raise AdmissionLeaseError("admission session scope is required")

    def cas(
        self,
        store: AdmittedControlStore,
        expected_revision: int,
        record: Mapping[str, object],
    ) -> dict[str, object]:
        """CAS a control record under the caller's admitted scope."""
        self._validate()
        return admitted_cas(
            store,
            expected_revision,
            record,
            self.lease,
            self.recheck,
            self.scope,
        )

    def publish_selector(
        self,
        path: Path,
        active_release: str,
        previous_release: str,
    ) -> None:
        """Publish a runtime selector under the same caller-owned evidence."""
        self._validate()
        # Import lazily so this contract remains independent of authority
        # implementation details and cannot create an import cycle.
        from tools.upgrade_authority import commit_runtime_selector_admitted

        commit_runtime_selector_admitted(
            path,
            active_release,
            previous_release,
            self.lease,
            self.recheck,
            self.scope,
        )
