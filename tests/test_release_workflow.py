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


def test_release_workflow_accepts_one_bounded_transient_transition_without_dirtying_source() -> (
    None
):
    assert "transition_json:" in WORKFLOW
    assert 'if [[ -n "$TRANSITION_PATH" && -n "$TRANSITION_JSON" ]]; then' in WORKFLOW
    assert "*$'\\n'*|*$'\\r'*|/*|*..*" in WORKFLOW
    assert 'echo "select exactly one transition input" >&2' in WORKFLOW
    assert 'transition_dir="$RUNNER_TEMP/release-transition-input"' in WORKFLOW
    assert 'install -d -m 700 "$transition_dir"' in WORKFLOW
    assert 'printf \'%s\' "$TRANSITION_JSON" > "$TRANSITION"' in WORKFLOW
    assert 'chmod 600 "$TRANSITION"' in WORKFLOW
    assert 'TRANSITION_WORKSPACE="$transition_dir"' in WORKFLOW
    assert 'printf \'TRANSITION_MODE=%s\\n\' "$TRANSITION_MODE" >> "$GITHUB_ENV"' in WORKFLOW
    assert 'TRANSITION="$RUNNER_TEMP/release-clone/$TRANSITION_PATH"' in WORKFLOW
    assert 'TRANSITION_WORKSPACE="$RUNNER_TEMP/release-clone"' in WORKFLOW
    assert '--workspace "$TRANSITION_WORKSPACE" --candidate' in WORKFLOW
    assert 'if [[ -n "$(git status --porcelain=v1)" ]]; then' in WORKFLOW
