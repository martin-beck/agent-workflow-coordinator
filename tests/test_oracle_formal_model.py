# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Regression checks that the oracle TLC model admits every gate outcome."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODEL = (ROOT / "formal/oracle/OracleInteractionGates.tla").read_text(encoding="utf-8")
CONFIG = (ROOT / "formal/oracle/OracleInteractionGates.cfg").read_text(encoding="utf-8")


def test_oracle_model_is_admitted_to_fast_tier() -> None:
    verify = (ROOT / "formal/handoffctl/verify.sh").read_text(encoding="utf-8")
    assert "run_model OracleInteractionGates" in verify
    assert '"OracleInteractionGates"' in (ROOT / "formal/tier-evidence.json").read_text(
        encoding="utf-8"
    )


def test_oracle_model_checks_completion_prefix_and_reachable_outcomes() -> None:
    assert "INVARIANTS TypeOK NoSkippedGate RevisionMonotonic CompletedPrefix" in CONFIG
    assert "ResolveAccepted(p)" in MODEL
    assert "ResolveUnresolved(p)" in MODEL
    assert 'operation\' = [operation EXCEPT ![p] = "resolve"]' in MODEL
    assert 'operation\' = [operation EXCEPT ![p] =' in MODEL
    assert 'disposition\' = [disposition EXCEPT ![p] = "accepted"]' in MODEL


def test_oracle_model_keeps_hostile_and_stale_paths_rejected() -> None:
    assert "Hostile(p) == operation[p] = \"open\"" in MODEL
    assert 'result\' = [result EXCEPT ![p] = "rejected"]' in MODEL
    assert "Stale(p) == expectedRevision[p] # revision" in MODEL
