# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Contract checks for the non-publishing release signer readiness gate."""

from pathlib import Path

WORKFLOW = (Path(__file__).parents[1] / ".github/workflows/release.yml").read_text(encoding="utf-8")


def test_release_workflow_probes_external_signer_without_publishing() -> None:
    assert "workflow_dispatch:" in WORKFLOW
    assert "AWC_RELEASE_SIGNER_PATH" in WORKFLOW
    assert "awc-sign-release.sh" in WORKFLOW
    assert '"$signer_path" --self-test' in WORKFLOW
    assert "creates tags" in WORKFLOW
    assert "pushes refs" in WORKFLOW


def test_signer_probe_precedes_release_contract_generation() -> None:
    assert WORKFLOW.index("Check authorized release signer") < WORKFLOW.index(
        "Generate and validate release contract"
    )
