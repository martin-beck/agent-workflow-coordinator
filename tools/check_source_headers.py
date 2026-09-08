# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Verify exact license headers on every tracked coordinator source file."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

COPYRIGHT = "Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved."
SPDX = "SPDX-License-Identifier: MIT"
COMMENT_PREFIXES = {".py": "# ", ".sh": "# ", ".tla": r"\* "}
EXTENSIONLESS_SOURCES = {Path("tools/handoffctl"): "# "}


class HeaderCheckError(RuntimeError):
    """Report a repository or source-decoding error."""


def comment_prefix(path: Path) -> str | None:
    """Return the required line-comment prefix for a tracked source path."""
    return EXTENSIONLESS_SOURCES.get(path, COMMENT_PREFIXES.get(path.suffix))


def tracked_source_files(root: Path) -> tuple[Path, ...]:
    """Return source files selected from Git's tracked-file set."""
    result = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=False, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise HeaderCheckError(f"git ls-files failed: {detail}")
    paths = (Path(raw.decode("utf-8")) for raw in result.stdout.split(b"\0") if raw)
    return tuple(path for path in paths if comment_prefix(path) is not None)


def check_file(root: Path, path: Path) -> list[str]:
    """Return exact-header violations for one selected source file."""
    prefix = comment_prefix(path)
    if prefix is None:
        raise HeaderCheckError(f"unsupported source path: {path}")
    try:
        lines = (root / path).read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as error:
        return [f"{path}: source is not valid UTF-8: {error}"]
    start = 1 if lines and lines[0].startswith("#!") else 0
    expected = [prefix + COPYRIGHT, prefix + SPDX]
    issues: list[str] = []
    if lines[start : start + 2] != expected:
        issues.append(f"{path}: expected exact Huawei/MIT header at line {start + 1}")
    for line, label in ((expected[0], "copyright"), (expected[1], "SPDX")):
        if lines.count(line) != 1:
            issues.append(f"{path}: expected exactly one canonical {label} line")
    return issues


def main(argv: list[str] | None = None) -> int:
    """Check the repository and print actionable violations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        sources = tracked_source_files(root)
        issues = [issue for path in sources for issue in check_file(root, path)]
    except HeaderCheckError as error:
        print(f"header check failed: {error}", file=sys.stderr)
        return 2
    if issues:
        print("\n".join(issues), file=sys.stderr)
        return 1
    print(f"Verified Huawei/MIT headers in {len(sources)} tracked source files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
