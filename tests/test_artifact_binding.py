# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Positive and hostile tests for revision-bound artifact bindings."""

import unittest

from tools.artifact_binding import (
    ARTIFACT_TYPES,
    ArtifactBinding,
    ArtifactBindingError,
    ArtifactSnapshot,
    ArtifactType,
    binding_errors,
)


def snapshot(artifact_type: str, *, version: int = 1, digest: str | None = None) -> ArtifactSnapshot:
    return ArtifactSnapshot(
        artifact_type=ArtifactType(artifact_type),
        ref=f"artifacts/{artifact_type}",
        digest=digest or "sha256:" + "a" * 64,
        scope="AR-0023",
        task_revision=4,
        version=version,
        predecessors=("oracle/discussion-1",),
    )


def binding(*, action: str = "record", version: int = 1, reopened: tuple[str, ...] = ()) -> ArtifactBinding:
    items = tuple(snapshot(item, version=version) for item in ARTIFACT_TYPES)
    return ArtifactBinding(
        task_id="AR-0023",
        task_revision=4,
        before=items,
        after=items,
        action=action,
        reopened_dependents=reopened,
    )


class ArtifactBindingTests(unittest.TestCase):
    def test_complete_binding_is_typed_revision_bound_and_digestable(self) -> None:
        value = binding()
        self.assertTrue(value.digest.startswith("sha256:"))
        restored = ArtifactBinding(
            task_id="AR-0023",
            task_revision=4,
            before=tuple(ArtifactSnapshot.from_record(item) for item in value.as_record()["before"]),
            after=tuple(ArtifactSnapshot.from_record(item) for item in value.as_record()["after"]),
        )
        self.assertEqual(value.digest, restored.digest)

    def test_missing_type_and_wrong_revision_are_rejected(self) -> None:
        items = tuple(snapshot(item) for item in ARTIFACT_TYPES[:-1])
        with self.assertRaisesRegex(ArtifactBindingError, "incomplete"):
            ArtifactBinding("AR-0023", 4, items, items)
        wrong = tuple(
            ArtifactSnapshot(
                item.artifact_type, item.ref, item.digest, item.scope, 3, item.version, item.predecessors
            )
            for item in (snapshot(name) for name in ARTIFACT_TYPES)
        )
        with self.assertRaisesRegex(ArtifactBindingError, "revision"):
            ArtifactBinding("AR-0023", 4, wrong, wrong)

    def test_changed_design_requires_explicit_reopen_and_dependents(self) -> None:
        before = tuple(snapshot(item) for item in ARTIFACT_TYPES)
        after = tuple(snapshot(item, version=2) for item in ARTIFACT_TYPES)
        with self.assertRaisesRegex(ArtifactBindingError, "explicit reopen"):
            ArtifactBinding("AR-0023", 4, before, after)
        with self.assertRaisesRegex(ArtifactBindingError, "dependent"):
            ArtifactBinding("AR-0023", 4, before, after, action="reopen")
        reopened = ArtifactBinding(
            "AR-0023", 4, before, after, action="reopen", reopened_dependents=("AR-0024",)
        )
        self.assertEqual(["AR-0024"], list(reopened.reopened_dependents))

    def test_serialized_binding_is_fail_closed(self) -> None:
        value = binding().as_record()
        self.assertEqual([], binding_errors(value))
        malformed = dict(value)
        malformed.pop("after")
        self.assertTrue(binding_errors(malformed))
        malformed = binding().as_record()
        malformed["before"] = malformed["before"][:-1]
        self.assertIn("incomplete", binding_errors(malformed)[0])


if __name__ == "__main__":
    unittest.main()
