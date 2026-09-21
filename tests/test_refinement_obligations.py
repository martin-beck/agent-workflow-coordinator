# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Machine-check the bounded formal refinement obligation matrix."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "formal" / "upgrade" / "refinement-obligations.json"
CONTRACT = ROOT / "formal" / "upgrade" / "v10-refinement-contract.json"


def _test_method_exists(reference: str) -> bool:
    path_text, selector = reference.split("::", 1)
    path = ROOT / path_text
    if not path.is_file():
        return False
    method = selector.rsplit(".", 1)[-1]
    return re.search(rf"\b{re.escape(method)}\b", path.read_text(encoding="utf-8")) is not None


def test_matrix_is_bound_to_the_exact_contract_and_fails_closed() -> None:
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    assert matrix["contract"] == "formal/upgrade/v10-refinement-contract.json"
    assert matrix["contract_sha256"] == hashlib.sha256(CONTRACT.read_bytes()).hexdigest()
    assert matrix["decision"] == "deny"
    assert "The matrix is not an implementation-refinement proof." in matrix["nonclaims"]
    assert matrix["obligations"]
    assert all(item["status"] != "proven" for item in matrix["obligations"])


def test_every_obligation_has_unique_model_actions_and_resolvable_evidence() -> None:
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    ids = [item["id"] for item in matrix["obligations"]]
    assert len(ids) == len(set(ids))
    actions: list[str] = []
    for item in matrix["obligations"]:
        assert item["model_actions"]
        assert item["evidence"]
        actions.extend(item["model_actions"])
        assert all(_test_method_exists(reference) for reference in item["evidence"])
    assert len(actions) == len(set(actions))


def test_contract_mutation_gate_is_rejection_only_while_matrix_is_unproven() -> None:
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert matrix["decision"] == "deny"
    assert "rejection-only" in contract["mutation_gate"]
