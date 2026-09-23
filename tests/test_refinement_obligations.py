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


def test_git_mutation_boundary_maps_backup_commit_and_rollback_without_authorizing_them() -> None:
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    entry = next(item for item in matrix["obligations"] if item["id"] == "git-mutation-boundary")
    assert entry["model_actions"] == ["Backup", "Commit", "Rollback"]
    assert entry["status"] == "not-proven"
    assert len(entry["evidence"]) >= 12
    assert any("generated_backup_executor" in reference for reference in entry["evidence"])
    assert any("GitCommitCapabilityTests" in reference for reference in entry["evidence"])
    assert any("bound_rollback_rejects" in reference for reference in entry["evidence"])
    assert (
        "No selector publication, runtime replacement, commit, apply, rollback, or release "
        "publication is authorized." in matrix["nonclaims"]
    )


def test_sqlite_write_fence_maps_every_route_class_without_authorizing_mutation() -> None:
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    entry = next(item for item in matrix["obligations"] if item["id"] == "sqlite-write-fence")
    assert entry["model_actions"] == ["RequestWrite", "AcceptWrite", "RejectWrite", "FinishWrite"]
    assert entry["status"] == "not-proven"
    assert len(entry["evidence"]) >= 12
    assert any("test_inventory_is_explicit" in reference for reference in entry["evidence"])
    assert any("every_inventoried_route_rejects" in reference for reference in entry["evidence"])
    assert any("sigkill_after_route_effects" in reference for reference in entry["evidence"])
    assert (
        "Static route coverage does not prove runtime refinement or authorize upgrade mutation."
        in json.loads(
            (ROOT / "formal/upgrade/sqlite-route-inventory.json").read_text(encoding="utf-8")
        )["nonclaims"]
    )


def test_functional_availability_maps_reopen_and_recovery_without_authorizing_mutation() -> None:
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    entry = next(item for item in matrix["obligations"] if item["id"] == "functional-availability")
    assert entry["model_actions"] == ["FunctionalAvailability", "Reopen"]
    assert entry["status"] == "not-proven"
    assert len(entry["evidence"]) >= 7
    assert any(
        "test_reopen_requires_explicit_functional_availability" in reference
        for reference in entry["evidence"]
    )
    assert any("fresh_capability_reopens" in reference for reference in entry["evidence"])
    assert any("requires_recovery_and_new_fence" in reference for reference in entry["evidence"])


def test_selector_execution_binding_maps_read_only_identity_evidence_without_dispatch() -> None:
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    entry = next(
        item for item in matrix["obligations"] if item["id"] == "selector-to-execution-binding"
    )
    assert entry["model_actions"] == ["ObserveSelector", "ValidateRuntime", "Dispatch"]
    assert entry["status"] == "not-proven"
    assert len(entry["evidence"]) >= 10
    assert any("SelectorAdmissionTests" in reference for reference in entry["evidence"])
    assert any("RuntimeAdmissionTests" in reference for reference in entry["evidence"])
    assert any(
        "test_snapshot_bound_rechecks_session" in reference for reference in entry["evidence"]
    )
    assert any(
        "test_snapshot_bound_rejects_non_read_only_backend_result" in reference
        for reference in entry["evidence"]
    )
    assert any(
        "test_apply_and_rollback_reject_both_backends_without_writes" in reference
        for reference in entry["evidence"]
    )
    assert any(
        "test_contract_file_and_dispatch_boundaries_fail_closed" in reference
        for reference in entry["evidence"]
    )
