# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Deterministic property coverage for bounded issue-series DAG manifests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.issue_series import IssueSeriesValidationError, parse_manifest
from tools.issue_series_plan import plan_issue_series


def manifest(seed: int, size: int) -> dict[str, object]:
    identifiers = [f"AR-{9000 + index:04d}" for index in range(size)]
    members: list[dict[str, object]] = []
    for index, identifier in enumerate(identifiers):
        dependencies = (
            [
                {"id": dependency, "rationale": "bounded predecessor"}
                for dependency in identifiers[:index]
            ]
            if index == size - 1
            else [
                {"id": dependency, "rationale": "bounded predecessor"}
                for dependency_index, dependency in enumerate(identifiers[:index])
                if (seed + index + dependency_index) % 2
            ]
        )
        members.append(
            {
                "id": identifier,
                "short_title": f"Bounded step {index + 1}",
                "scope": f"Exercise bounded series member {index + 1}",
                "acceptance": "Deterministic property assertion passes",
                "ownership": "Coordinator core",
                "depends_on": dependencies,
            }
        )
    return {
        "schema_version": 1,
        "repository": "github.com/example/coordinator",
        "issue": {
            "number": seed + 1,
            "url": f"https://github.com/example/coordinator/issues/{seed + 1}",
        },
        "issue_digest": "sha256:" + f"{seed:064x}",
        "members": members,
        "finalizer": identifiers[-1],
    }


class IssueSeriesPropertyTests(unittest.TestCase):
    def test_generated_dags_preserve_finalizer_closure_and_canonical_receipts(self) -> None:
        for seed in range(16):
            for size in range(2, 9):
                with self.subTest(seed=seed, size=size):
                    value = manifest(seed, size)
                    parsed = parse_manifest(value)
                    self.assertEqual(
                        {edge.id for edge in parsed.members[-1].depends_on},
                        {member.id for member in parsed.members[:-1]},
                    )
                    with TemporaryDirectory() as directory:
                        path = Path(directory) / "series.json"
                        path.write_text(json.dumps(value))
                        receipt = json.loads(
                            plan_issue_series(
                                path,
                                expected_product_slug="example/coordinator",
                                existing_task_ids=(),
                                existing_issue_series_identities=(),
                                current_state_digest="sha256:" + "0" * 64,
                            )
                        )
                    self.assertEqual(receipt["task_ids"], [member.id for member in parsed.members])
                    self.assertEqual(receipt["manifest"]["finalizer"], parsed.finalizer)

    def test_hostile_mutations_are_rejected_for_every_generated_shape(self) -> None:
        for mutation in ("duplicate", "private", "long-title", "cycle"):
            with self.subTest(mutation=mutation):
                value = manifest(7, 4)
                members = value["members"]
                assert isinstance(members, list)
                if mutation == "duplicate":
                    members[1]["id"] = members[0]["id"]
                elif mutation == "private":
                    members[0]["scope"] = "Read /" + "home/private"
                elif mutation == "long-title":
                    members[0]["short_title"] = "This title has five words"
                else:
                    members[0]["depends_on"] = [{"id": members[-1]["id"], "rationale": "cycle"}]
                with self.assertRaises(IssueSeriesValidationError):
                    parse_manifest(value)
