# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for the narrow lifecycle mutation admission seam."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import mutation_admission


def test_admission_seam_forwards_ledger_policy(tmp_path: Path) -> None:
    with patch.object(mutation_admission, "task_spec_errors", return_value=[]) as validate:
        assert (
            mutation_admission.task_errors(
                tmp_path, {"id": "AR-0001"}, None, include_ledger=False
            )
            == []
        )
        assert mutation_admission.task_errors(tmp_path, {"id": "AR-0001"}, None) == []
    assert [call.kwargs["include_ledger"] for call in validate.call_args_list] == [False, True]
