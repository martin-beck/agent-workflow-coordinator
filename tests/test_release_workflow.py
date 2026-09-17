# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Contract checks for the non-publishing release signer readiness gate."""

from pathlib import Path

WORKFLOW = (Path(__file__).parents[1] / ".github/workflows/release.yml").read_text(encoding="utf-8")


def test_release_workflow_probes_external_signer_without_publishing() -> None:
    assert "workflow_dispatch:" in WORKFLOW
    assert "awc-sign-release.sh" in WORKFLOW
    assert '"$signer_dir/awc-sign-release.sh" --self-test' in WORKFLOW
    assert "GITHUB_STEP_SUMMARY" in WORKFLOW
    assert "ephemeral self-test only" in WORKFLOW
    assert '"@SOURCE_COMMIT@": transition["to"]["source_commit"]' in WORKFLOW
    assert "do not sign" in WORKFLOW
    assert "do not" in WORKFLOW


def test_release_signer_template_is_self_test_only() -> None:
    template = (Path(__file__).parents[1] / "docs/templates/awc-sign-release.sh.in").read_text(
        encoding="utf-8"
    )
    assert '"--self-test"' in template
    assert "tag -s" in template
    assert "@REPOSITORY@" in template
    assert "@FROM_TAG@" in template
    assert "@TO_TAG@" in template
    assert "@TRUST_POLICY@" in template
    assert "@VENDOR_MANIFEST@" in template
    assert "@OUTPUT@" in template
    assert "rev-parse HEAD" in template
    assert "@RELEASE_VERSION@" in template
    assert "@SOURCE_COMMIT@" in template
    assert "sign" in template.lower()
    assert "git push" not in template
    assert "contract self-test passed" in template
    assert 'echo "release signer contract self-test passed"' in template
    assert 'echo "release signer contract ready:' not in template


def test_signer_probe_precedes_release_contract_generation() -> None:
    assert WORKFLOW.index("Check authorized release signer") < WORKFLOW.index(
        "Generate and validate release contract"
    )
