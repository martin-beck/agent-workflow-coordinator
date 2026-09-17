# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Offline acceptance trace for the complete Coordinator/AWG/AWQ TUI path."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from tools.integration_contract import Evidence, EvidenceKind, IntegrationContract, IntegrationError

TRACE_STEPS = (
    "document_switch",
    "anchor_highlight",
    "batch_selection",
    "proposal_evaluation",
    "formal_specification",
    "user_disposition",
    "safe_exit",
    "reask",
    "future_ar_capture",
    "reconciliation",
    "implementation_handoff",
)
PUBLIC_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


class TuiIntegrationError(ValueError):
    """A bypassed gate, malformed trace, or authority-boundary violation."""


class Initiator(StrEnum):
    AGENT = "agent"
    USER = "user"


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    task_id: str
    initiator: Initiator
    final_revision: int
    contract_digest: str
    future_ar_ref: str


@dataclass(frozen=True, slots=True)
class TuiAcceptanceTrace:
    task_id: str
    task_revision: int
    initiator: Initiator
    steps: tuple[str, ...]
    formal_spec_ref: str
    user_disposition_ref: str
    quality_evidence_ref: str
    future_ar_ref: str
    implementation_ref: str
    decision_authority: str = "coordinator"

    def accept(self) -> AcceptanceResult:
        """Validate the complete path without executing a provider or storing text."""
        if not re.fullmatch(r"AR-\d{4}", self.task_id) or self.task_revision < 1:
            raise TuiIntegrationError("task identity or revision is invalid")
        if tuple(self.steps) != TRACE_STEPS:
            missing = next((step for step in TRACE_STEPS if step not in self.steps), "unknown")
            raise TuiIntegrationError(f"integration trace bypasses {missing.replace('_', ' ')}")
        if self.decision_authority != "coordinator":
            raise TuiIntegrationError("only Coordinator owns task transition authority")
        for value, label in (
            (self.formal_spec_ref, "formal specification"),
            (self.user_disposition_ref, "user disposition"),
            (self.quality_evidence_ref, "quality evidence"),
            (self.future_ar_ref, "future AR"),
            (self.implementation_ref, "implementation"),
        ):
            _ref(value, label)
        if not re.fullmatch(r"AR-\d{4}", self.future_ar_ref):
            raise TuiIntegrationError("future AR mapping is invalid")
        try:
            contract = IntegrationContract(
                self.task_id,
                self.task_revision,
                Evidence(
                    "guidance",
                    EvidenceKind.GUIDANCE,
                    "awg/guidance/trace",
                    _digest("a"),
                    self.task_revision,
                ),
                Evidence(
                    "quality",
                    EvidenceKind.QUALITY,
                    self.quality_evidence_ref,
                    _digest("b"),
                    self.task_revision,
                ),
                Evidence(
                    "quality",
                    EvidenceKind.FORMAL,
                    self.formal_spec_ref,
                    _digest("c"),
                    self.task_revision,
                ),
                "coordinator/events/tui-acceptance",
                self.decision_authority,
            )
        except IntegrationError as error:
            raise TuiIntegrationError(f"cross-project evidence is invalid: {error}") from error
        return AcceptanceResult(
            self.task_id,
            self.initiator,
            self.task_revision + len(TRACE_STEPS),
            contract.digest,
            self.future_ar_ref,
        )


def _ref(value: str, label: str) -> None:
    if not isinstance(value, str) or not PUBLIC_REF.fullmatch(value):
        raise TuiIntegrationError(f"{label} must be public-safe")
    if value.startswith("/") or ".." in value or "//" in value:
        raise TuiIntegrationError(f"{label} must be public-safe")


def _digest(char: str) -> str:
    return "sha256:" + char * 64
