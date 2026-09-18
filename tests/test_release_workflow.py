# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Contract checks for the non-publishing release tag readiness gate."""

from pathlib import Path

WORKFLOW = (Path(__file__).parents[1] / ".github/workflows/release.yml").read_text(encoding="utf-8")


def test_release_workflow_prepares_external_tag_without_publishing() -> None:
    assert "workflow_dispatch:" in WORKFLOW
    assert "Release tag command (copy/paste" in WORKFLOW
    assert "not executed by CI" in WORKFLOW
    assert "HEAD equals the transition source commit" in WORKFLOW
    assert "GITHUB_STEP_SUMMARY" in WORKFLOW
    assert "--candidate" in WORKFLOW
    assert 'transition_path.read_text(encoding="utf-8")' in WORKFLOW
    assert 'Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a"' in WORKFLOW
    assert "Not executed by CI" in WORKFLOW
    assert "/home/martin" not in WORKFLOW
    assert '"git", "tag"' in WORKFLOW
    assert '"git", "push", "origin"' in WORKFLOW


def test_tag_command_is_after_release_contract_validation() -> None:
    assert WORKFLOW.index("Prepare release tag command") > WORKFLOW.index(
        "Generate and validate release contract"
    )
