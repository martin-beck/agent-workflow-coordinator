# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Process launch support for the ephemeral publication credential descriptor."""

from __future__ import annotations

import argparse
import subprocess


def run_envelope_subprocess(
    command: list[str], timeout: float, args: argparse.Namespace
) -> subprocess.CompletedProcess[bytes]:
    """Launch a sandboxed command while preserving only explicitly admitted FDs."""
    if hasattr(args, "_envelope_worktree_fd"):
        pass_fds = [int(args._envelope_worktree_fd)]
        credential_fd = getattr(args, "credential_fd", None)
        if credential_fd is not None:
            pass_fds.append(int(credential_fd))
        return subprocess.run(  # noqa: S603
            command,
            cwd=str(args._envelope_worktree),
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            pass_fds=tuple(pass_fds),
        )
    return subprocess.run(  # noqa: S603
        command, check=False, stdin=subprocess.DEVNULL, timeout=timeout
    )
