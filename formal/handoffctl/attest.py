# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Emit a machine-readable, tier-specific formal execution attestation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", choices=("portable-smoke", "full-exhaustive"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jar", type=Path, required=True)
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument(
        "--status", choices=("success", "oom", "timeout", "canceled", "incomplete"), default="success"
    )
    args = parser.parse_args()
    if args.status != "success":
        parser.error("failed or incomplete formal runs cannot produce a success attestation")
    if args.tier == "portable-smoke" and len(args.models) != 1:
        parser.error("portable-smoke attestation must contain exactly one model")
    if args.tier == "full-exhaustive" and len(args.models) != 6:
        parser.error("full-exhaustive attestation must contain all six models")
    root = Path(__file__).resolve().parents[2]
    configs = {model: digest(root / "formal" / "handoffctl" / f"{model}.cfg") for model in args.models}
    models = {model: digest(root / "formal" / "handoffctl" / f"{model}.tla") for model in args.models}
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=root, text=True).strip()
    formal_hash = hashlib.sha256(
        json.dumps({"models": models, "configs": configs}, sort_keys=True).encode()
    ).hexdigest()
    result = {
        "schema_version": 1,
        "profile": args.tier,
        "exhaustive": args.tier == "full-exhaustive",
        "commit": commit,
        "tree": tree,
        "formal_input_sha256": formal_hash,
        "models": models,
        "configs": configs,
        "tool_jar_sha256": digest(args.jar),
        "resource_bounds": {
            "workers": 2,
            "heap": os.environ.get("TLC_HEAP", "2048m"),
            "memory_max": "3G",
            "swap_max": "3G",
            "admission": "portable-or-systemd-fail-closed",
        },
        "outcomes": {model: "success" for model in args.models},
        "state_counts": {model: None for model in args.models},
        "status": "success",
        "timestamp_epoch": int(time.time()),
        "freshness_seconds": 0,
        "non_claims": [
            "portable-smoke is non-exhaustive and cannot support full formal claims",
            "bounded model checking does not prove implementation correspondence",
        ],
    }
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
