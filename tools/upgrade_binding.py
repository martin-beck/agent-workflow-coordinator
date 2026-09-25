# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Explicit binding between a portable upgrade contract and runtime identity."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

if __package__:
    from .upgrade_contract_runtime import RuntimeContractError, validate_runtime_contract
    from .upgrade_identity import ENVELOPE_FIELDS, UpgradeIdentityError, validate_envelope
else:  # pragma: no cover - direct vendored script imports
    from upgrade_contract_runtime import (  # type: ignore[import-not-found,no-redef]
        RuntimeContractError,
        validate_runtime_contract,
    )
    from upgrade_identity import (  # type: ignore[import-not-found,no-redef]
        ENVELOPE_FIELDS,
        UpgradeIdentityError,
        validate_envelope,
    )

BINDING_SCHEMA_VERSION = 1
BINDING_FIELDS = (
    "schema_version",
    "contract_digest",
    "session_identity_digest",
    "contract_operation_id",
    "contract_backend",
    "contract_selector_ref",
    "contract_expected_state_revision",
    "contract_barrier_id",
    "contract_fencing_token",
    "contract_backup_operation_id",
    "runtime_envelope",
)
_DIGEST = re.compile(r"[0-9a-f]{64}")


class UpgradeBindingError(ValueError):
    """A portable contract and host-bound runtime identity do not match."""


def canonical_contract_digest(contract: Mapping[str, object]) -> str:
    """Return the digest of the exact validated portable contract."""
    try:
        encoded = json.dumps(
            contract, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise UpgradeBindingError("upgrade contract is not canonical JSON") from error
    return hashlib.sha256(encoded).hexdigest()


def _contract_inputs(contract: Mapping[str, Any]) -> dict[str, Any]:
    rollback = contract["rollback"]
    operation = rollback["operation"]
    inputs = operation["inputs"]
    if not isinstance(inputs, dict):
        raise UpgradeBindingError("upgrade rollback operation inputs are invalid")
    return inputs


def _validate_contract_identity(contract: Mapping[str, Any]) -> dict[str, Any]:
    try:
        value = validate_runtime_contract(contract)
    except RuntimeContractError as error:
        raise UpgradeBindingError("upgrade contract is invalid") from error
    inputs = _contract_inputs(value)
    required = {
        "backend",
        "selector_ref",
        "expected_state_revision",
        "barrier_id",
        "fencing_token",
        "backup_operation_id",
    }
    if set(inputs) != required:
        raise UpgradeBindingError("upgrade contract binding inputs are invalid")
    for phase in value["phases"]:
        if phase["operation"]["inputs"] != inputs:
            raise UpgradeBindingError("upgrade phase inputs do not match rollback inputs")
    if value["backend"] != inputs["backend"]:
        raise UpgradeBindingError("upgrade contract backend binding is inconsistent")
    if inputs["backup_operation_id"] != f"{value['operation_id']}:backup":
        raise UpgradeBindingError("upgrade backup operation identity is invalid")
    if value["rollback"]["operation"]["operation_id"] != f"{value['operation_id']}:rollback":
        raise UpgradeBindingError("upgrade rollback operation identity is invalid")
    return value


def _validated_envelope(value: Mapping[str, object]) -> dict[str, object]:
    try:
        return validate_envelope(value)
    except UpgradeIdentityError as error:
        raise UpgradeBindingError("runtime upgrade envelope is invalid") from error


@dataclass(frozen=True, slots=True)
class UpgradeRuntimeBinding:
    """Validated contract/runtime identity; this type performs no dispatch."""

    schema_version: int
    contract_digest: str
    session_identity_digest: str
    contract_operation_id: str
    contract_backend: str
    contract_selector_ref: str
    contract_expected_state_revision: int
    contract_barrier_id: str
    contract_fencing_token: str
    contract_backup_operation_id: str
    runtime_envelope: dict[str, object]

    @classmethod
    def bind(
        cls,
        contract: Mapping[str, object],
        runtime_envelope: Mapping[str, object],
        *,
        session_identity_digest: str,
    ) -> UpgradeRuntimeBinding:
        value = _validate_contract_identity(contract)
        envelope = _validated_envelope(runtime_envelope)
        inputs = _contract_inputs(value)
        if _DIGEST.fullmatch(session_identity_digest) is None:
            raise UpgradeBindingError("runtime barrier session identity digest is invalid")
        if envelope["target"] != "rollback":
            raise UpgradeBindingError("runtime binding target must be rollback")
        if envelope["operation_id"] != value["operation_id"]:
            raise UpgradeBindingError("runtime operation identity does not match contract")
        if envelope["backend"] != inputs["backend"]:
            raise UpgradeBindingError("runtime backend identity does not match contract")
        for envelope_field, input_field in (
            ("selector_ref", "selector_ref"),
            ("state_revision", "expected_state_revision"),
            ("durable_barrier_id", "barrier_id"),
            ("fencing_token", "fencing_token"),
        ):
            if envelope[envelope_field] != inputs[input_field]:
                raise UpgradeBindingError(f"runtime {envelope_field} does not match contract")
        return cls(
            BINDING_SCHEMA_VERSION,
            canonical_contract_digest(value),
            session_identity_digest,
            value["operation_id"],
            inputs["backend"],
            inputs["selector_ref"],
            inputs["expected_state_revision"],
            inputs["barrier_id"],
            inputs["fencing_token"],
            inputs["backup_operation_id"],
            envelope,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> UpgradeRuntimeBinding:
        if set(value) != set(BINDING_FIELDS):
            raise UpgradeBindingError("runtime binding fields are invalid")
        if value["schema_version"] != BINDING_SCHEMA_VERSION:
            raise UpgradeBindingError("runtime binding schema version is invalid")
        envelope = value["runtime_envelope"]
        if not isinstance(envelope, Mapping) or set(envelope) != set(ENVELOPE_FIELDS):
            raise UpgradeBindingError("runtime binding envelope is invalid")
        for field in (
            "contract_digest",
            "session_identity_digest",
            "contract_operation_id",
            "contract_backend",
            "contract_selector_ref",
            "contract_barrier_id",
            "contract_fencing_token",
            "contract_backup_operation_id",
        ):
            if not isinstance(value[field], str) or not value[field]:
                raise UpgradeBindingError(f"runtime binding {field} is invalid")
        if _DIGEST.fullmatch(cast(str, value["contract_digest"])) is None:
            raise UpgradeBindingError("runtime binding contract digest is invalid")
        if _DIGEST.fullmatch(cast(str, value["session_identity_digest"])) is None:
            raise UpgradeBindingError("runtime binding session identity digest is invalid")
        revision = value["contract_expected_state_revision"]
        if type(revision) is not int or revision < 1:
            raise UpgradeBindingError("runtime binding state revision is invalid")
        _validated_envelope(envelope)
        strings = {
            field: cast(str, value[field])
            for field in (
                "contract_digest",
                "session_identity_digest",
                "contract_operation_id",
                "contract_backend",
                "contract_selector_ref",
                "contract_barrier_id",
                "contract_fencing_token",
                "contract_backup_operation_id",
            )
        }
        return cls(
            value["schema_version"],
            strings["contract_digest"],
            strings["session_identity_digest"],
            strings["contract_operation_id"],
            strings["contract_backend"],
            strings["contract_selector_ref"],
            revision,
            strings["contract_barrier_id"],
            strings["contract_fencing_token"],
            strings["contract_backup_operation_id"],
            dict(envelope),
        )

    def as_mapping(self) -> dict[str, object]:
        return {field: getattr(self, field) for field in BINDING_FIELDS}

    def validate_live_session(self, session: object) -> None:
        """Validate the observed held durable session without changing it."""
        from tools.rollback_control_store import BarrierSessionState

        if not isinstance(session, BarrierSessionState):
            raise UpgradeBindingError("live rollback barrier session is invalid")
        identity = session.identity
        envelope = self.runtime_envelope
        if identity.identity_digest != self.session_identity_digest:
            raise UpgradeBindingError("live barrier session identity digest does not match")
        for session_field, envelope_field in (
            ("project_id", "project_id"),
            ("state_revision", "state_revision"),
            ("authority_revision_at_acquire", "authority_revision"),
            ("durable_barrier_id", "durable_barrier_id"),
            ("fencing_token", "fencing_token"),
        ):
            if getattr(identity, session_field) != envelope[envelope_field]:
                raise UpgradeBindingError(f"live barrier {session_field} does not match")
        child = session.rollback_child
        if session.status != "held" or child is None:
            raise UpgradeBindingError("live rollback barrier is not held with a rollback child")
        if (
            child.operation_id != envelope["operation_id"]
            or child.target != "rollback"
            or child.barrier_identity_digest != identity.identity_digest
        ):
            raise UpgradeBindingError("live rollback child identity does not match")
