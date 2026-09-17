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
    assert "self-test only, no signing or publication" in WORKFLOW
    assert 'chmod 700 "$signer_dir"' in WORKFLOW
    assert "retained for this job" in WORKFLOW
    assert "trap 'rm -rf" not in WORKFLOW
    assert '"@SOURCE_COMMIT@": transition["to"]["source_commit"]' in WORKFLOW
    assert 'transition_path.read_text(encoding="utf-8")' in WORKFLOW
    assert "no signing or publication" in WORKFLOW


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
    assert "@SIGNING_KEY_REF@" in template
    assert "@SIGNING_POLICY_REF@" in template
    assert "@SIGNING_IDENTITY@" in template
    assert "@SIGNING_KEY_FINGERPRINT@" in template
    assert "@SIGNING_POLICY_DIGEST@" in template
    assert "rev-parse HEAD" in template
    assert "@RELEASE_VERSION@" in template
    assert "@SOURCE_COMMIT@" in template
    assert "sign" in template.lower()
    assert "git push" not in template
    assert "contract self-test passed" in template
    assert 'echo "release signer contract self-test passed"' in template
    assert 'echo "release signer contract ready:' not in template
    assert "required release signer policy" in WORKFLOW
    assert "gpg.ssh.allowedSignersFile" in WORKFLOW
    assert "not authorized by the GitHub release-key policy" in WORKFLOW
    assert 'canonical_identity = "martin.beck2@gmx.de"' in WORKFLOW
    assert "does not match the canonical GitHub release identity" in WORKFLOW
    assert "ssh-keygen" in WORKFLOW
    assert 'config("gpg.format") != "ssh"' in WORKFLOW
    assert "config --get gpg.format" in template


def test_signer_probe_precedes_release_contract_generation() -> None:
    assert WORKFLOW.index("Render and self-test release signer") > WORKFLOW.index(
        "Generate and validate release contract"
    )
