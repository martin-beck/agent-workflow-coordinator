# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Regression tests for formal-sensitive Verify workflow scope admission."""

from pathlib import Path


def test_runtime_admission_changes_require_formal_verify() -> None:
    workflow = Path(".github/workflows/verify.yml").read_text(encoding="utf-8")
    assert "formal_changed=true" in workflow
    assert "tools/runtime_bootstrap.py" in workflow
