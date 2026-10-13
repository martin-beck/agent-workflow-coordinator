# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Regression tests for formal-sensitive Verify workflow scope admission."""

import unittest
from pathlib import Path


def test_runtime_admission_changes_require_formal_verify() -> None:
    workflow = Path(".github/workflows/verify.yml").read_text(encoding="utf-8")
    assert "formal_changed=true" in workflow
    assert "tools/runtime_bootstrap.py" in workflow


class AdmissionEnvironmentScopeTests(unittest.TestCase):
    def test_every_formal_capable_job_receives_one_private_run_admission_scope(self) -> None:
        workflow = Path(".github/workflows/verify.yml").read_text(encoding="utf-8")
        formal = Path(".github/workflows/formal.yml").read_text(encoding="utf-8")
        combined = workflow + formal
        self.assertEqual(4, combined.count("TLC_ADMISSION_QUEUE: ${{ github.workspace }}/"))
        self.assertEqual(4, combined.count("TLC_ADMISSION_LOCK: ${{ github.workspace }}/"))
        for name in ("TLC_ADMISSION_QUEUE", "TLC_ADMISSION_LOCK"):
            self.assertNotIn(f"{name}: /tmp/", combined)
            self.assertNotIn(f"{name}: queue", combined)
            self.assertNotIn(f"{name}: lock", combined)

    def test_formal_explicit_arguments_use_the_job_admission_scope(self) -> None:
        workflow = Path(".github/workflows/formal.yml").read_text(encoding="utf-8")
        self.assertIn('--diagnostic-queue "$TLC_ADMISSION_QUEUE"', workflow)
        self.assertIn('--diagnostic-admission-lock "$TLC_ADMISSION_LOCK"', workflow)
        self.assertNotIn('"${RUNNER_TEMP}/formal-queue"', workflow)
        self.assertNotIn('"${RUNNER_TEMP}/formal-admission.lock"', workflow)
