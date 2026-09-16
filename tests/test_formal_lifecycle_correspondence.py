# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Keep the documented backup lifecycle mapping aligned with the TLA model."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FormalLifecycleCorrespondenceTests(unittest.TestCase):
    def test_lifecycle_mapping_names_existing_model_actions(self) -> None:
        model = (ROOT / "formal/handoffctl/Handoffctl.tla").read_text(encoding="utf-8")
        mapping = (ROOT / "formal/handoffctl/LIFECYCLE_CORRESPONDENCE.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("ExecuteSuccess(p)", model)
        self.assertIn("ExecuteReject(p)", model)
        self.assertIn("ExecuteSuccess(p)", mapping)
        self.assertIn("ExecuteReject(p)", mapping)

    def test_mapping_explicitly_remains_non_authorizing(self) -> None:
        mapping = (ROOT / "formal/handoffctl/LIFECYCLE_CORRESPONDENCE.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("through the phase machine", mapping)
        self.assertIn("execute rollback authorization", mapping)
