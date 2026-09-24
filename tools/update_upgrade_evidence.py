# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Refresh checked-in upgrade evidence digests from the current source tree."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import cast


def refresh(root: Path) -> dict[str, object]:
    """Refresh digests and bind the snapshot to the current Git revision."""
    path = root / "formal/upgrade/evidence.json"
    evidence = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    snapshot = cast(dict[str, object], evidence["implementation_snapshot"])
    files = cast(dict[str, str], snapshot["files"])
    for relative in files:
        files[relative] = hashlib.sha256((root / relative).read_bytes()).hexdigest()
    try:
        revision = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        revision = ""
    if revision:
        snapshot["revision"] = revision
        snapshot["runtime_revision"] = revision
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return evidence


if __name__ == "__main__":
    refresh(Path(__file__).resolve().parents[1])
