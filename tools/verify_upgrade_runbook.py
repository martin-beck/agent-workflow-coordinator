# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Fail-closed validation of checked-in release-specific upgrade runbooks."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .generate_upgrade_runbook import RunbookError, _read_contract, generate_runbooks


class RunbookVerificationError(ValueError):
    """Raised when generated runbook output is missing, stale, or not private."""


def _private_values(document: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for phase in document["phases"]:
        inputs = phase["operation"]["inputs"]
        values.update(
            str(inputs[field]) for field in ("selector_ref", "barrier_id", "fencing_token")
        )
    for release in (document["from"], document["to"]):
        values.update(
            str(release[field])
            for field in (
                "source_commit",
                "tag_object",
                "signature_sha256",
                "trust_policy_sha256",
                "vendor_manifest_sha256",
            )
        )
    return values


def verify_runbooks(document: dict[str, Any], output: Path) -> None:
    """Verify exact deterministic output and reject private contract values."""
    try:
        expected = generate_runbooks(document)
    except RunbookError as error:
        raise RunbookVerificationError("contract cannot generate runbooks") from error
    if output.is_symlink() or not output.is_dir():
        raise RunbookVerificationError("runbook output must be a non-aliased directory")
    actual: dict[str, str] = {}
    for name in expected:
        path = output / name
        if path.is_symlink() or not path.is_file():
            raise RunbookVerificationError(f"missing or unsafe generated output: {name}")
        try:
            actual[name] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise RunbookVerificationError(f"generated output is unreadable: {name}") from error
    if actual != expected:
        raise RunbookVerificationError("generated runbook output differs from the contract")
    private = _private_values(document)
    if any(value and value in content for value in private for content in actual.values()):
        raise RunbookVerificationError("generated output contains a private contract value")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contract", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    try:
        verify_runbooks(_read_contract(args.contract), args.output)
    except (OSError, RunbookError, RunbookVerificationError) as error:
        print(f"invalid upgrade runbooks: {error}", file=sys.stderr)
        return 1
    print("upgrade runbooks: valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
