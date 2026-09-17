# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Contract checks for the non-publishing release signer readiness gate."""

from pathlib import Path

WORKFLOW = (Path(__file__).parents[1] / ".github/workflows/release.yml").read_text(encoding="utf-8")


def test_release_workflow_probes_external_signer_without_publishing() -> None:
    assert "workflow_dispatch:" in WORKFLOW
    assert "awc-sign-release.sh" in WORKFLOW
    assert "Release signing command (copy/paste" in WORKFLOW
    assert "not executed by CI" in WORKFLOW
    assert "HEAD equals the transition source commit" in WORKFLOW
    assert "GITHUB_STEP_SUMMARY" in WORKFLOW
    assert "--backend" in WORKFLOW
    assert "--selector-ref" in WORKFLOW
    assert "--state-revision" in WORKFLOW
    assert "--barrier-id" in WORKFLOW
    assert "--fencing-token" in WORKFLOW
    assert "--operation-id" in WORKFLOW
    assert "AWC_TRUST_POLICY_FILE" in WORKFLOW
    assert 'transition_path.read_text(encoding="utf-8")' in WORKFLOW
    assert 'Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a"' in WORKFLOW
    assert "--push" not in WORKFLOW
    assert '.removeprefix("refs/tags/")' in WORKFLOW
    assert "Not executed by CI" in WORKFLOW
    assert "/home/martin" not in WORKFLOW
    assert '"--repo", "."' in WORKFLOW
    assert '"$AWC_RELEASE_OUTPUT"' in WORKFLOW
    assert "AWC_VENDOR_MANIFEST_FILE" in WORKFLOW
    assert '"--repo", os.environ["GITHUB_WORKSPACE"]' not in WORKFLOW
    assert '"--output", os.path.join(os.environ["RUNNER_TEMP"]' not in WORKFLOW


def test_signing_command_is_after_release_contract_validation() -> None:
    assert WORKFLOW.index("Prepare release signing command") > WORKFLOW.index(
        "Generate and validate release contract"
    )
