# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Canonical operation-domain configuration generation for full TLC slices."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

OPERATIONS = (
    "promote", "resume", "unblock", "pause", "claim", "heartbeat", "update", "accept",
    "bind-spec", "bind-worktree", "recover_expired", "release_planned", "release_open",
    "release_blocked", "release_done",
)


def render(base: str, operation: str) -> str:
    if operation not in OPERATIONS:
        raise ValueError(f"unknown operation slice: {operation}")
    all_operations = ", ".join(f'"{item}"' for item in OPERATIONS)
    replacement = (
        "    AllowedOperations = [p \\in {p1, p2} |-> IF p = p1 THEN "
        + '{"' + operation + '"} ELSE {' + all_operations + "}]"
    )
    result, count = re.subn(
        r"^    AllowedOperations = .*$", lambda _match: replacement, base, flags=re.MULTILINE
    )
    if count != 1:
        raise ValueError("base configuration must contain exactly one AllowedOperations assignment")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--operation", choices=OPERATIONS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = args.base.read_text(encoding="utf-8")
    args.output.write_text(render(base, args.operation), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
