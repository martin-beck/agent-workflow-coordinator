# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Contract checks for the non-publishing release signer readiness gate."""

import shlex
import subprocess
import tempfile
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


def test_rendered_signer_self_test_is_fixed_and_private_data_free() -> None:
    template = (Path(__file__).parents[1] / "docs/templates/awc-sign-release.sh.in").read_text(
        encoding="utf-8"
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        policy = root / "allowed-signers"
        policy.write_text("authorized@example.invalid ssh-ed25519 AAAA\n", encoding="utf-8")
        values = {
            "@RELEASE_VERSION@": "v9.9.9",
            "@SOURCE_COMMIT@": "a" * 40,
            "@REPOSITORY@": str(root),
            "@FROM_TAG@": "refs/tags/v9.9.8",
            "@TO_TAG@": "refs/tags/v9.9.9",
            "@TRUST_POLICY@": "b" * 64,
            "@VENDOR_MANIFEST@": "c" * 64,
            "@OUTPUT@": str(root / "out.json"),
            "@SIGNING_KEY_REF@": str(root / "release.pub"),
            "@SIGNING_POLICY_REF@": str(policy),
            "@SIGNING_IDENTITY@": "authorized@example.invalid",
            "@SIGNING_KEY_FINGERPRINT@": "SHA256:authorized",
            "@SIGNING_POLICY_DIGEST@": "d" * 64,
        }
        for placeholder, value in values.items():
            template = template.replace(placeholder, shlex.quote(value))
        script = root / "signer.sh"
        script.write_text(template, encoding="utf-8")
        script.chmod(0o700)
        result = subprocess.run(  # noqa: S603
            [str(script), "--self-test"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout == "release signer contract self-test passed\n"
        assert "v9.9.9" not in result.stdout
        assert str(root) not in result.stdout
