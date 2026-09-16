# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Generate and validate a release-bound upgrade contract for CI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.generate_upgrade_contract import generate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transition", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    transition = json.loads(args.transition.read_text(encoding="utf-8"))
    document = generate(transition)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
