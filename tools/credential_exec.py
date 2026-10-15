# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Run one publication command after an ephemeral FD-only gh login."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-fd", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.credential_fd < 3 or not args.command or args.command[0] == "--":
        parser.error("credential fd >= 3 and a command are required")
    try:
        with os.fdopen(args.credential_fd, "rb", closefd=False) as credential:
            gh = shutil.which("gh")
            if gh is None:
                return 127
            login = subprocess.run(  # noqa: S603
                [gh, "auth", "login", "--with-token"],
                stdin=credential,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    except OSError as error:
        print(f"credential handoff unavailable: {error}", file=sys.stderr)
        return 78
    if login.returncode:
        return int(login.returncode)
    os.close(args.credential_fd)
    os.execvpe(args.command[0], args.command, os.environ)  # noqa: S606
    raise RuntimeError("credential command exec returned unexpectedly")


if __name__ == "__main__":
    raise SystemExit(main())
