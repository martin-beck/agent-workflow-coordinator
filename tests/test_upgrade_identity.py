# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Exact canonical-envelope tests for upgrade and rollback identities."""

from __future__ import annotations

import unittest

from tools.upgrade_identity import (
    ENVELOPE_FIELDS,
    UpgradeIdentityError,
    canonical_barrier_digest,
    canonical_envelope_digest,
    validate_envelope,
)


def envelope() -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 2,
        "backend": "sqlite",
        "project_id": "11111111-1111-4111-8111-111111111111",
        "operation_id": "op-1",
        "state_revision": 1,
        "authority_revision": "authority-1",
        "fencing_token": "fence-1",
        "fencing_owner": "owner-1",
        "durable_barrier_id": "barrier-1",
        "artifact_root": "/artifacts",
        "source": "/authority.sqlite",
        "destination": "/artifacts/backup.sqlite",
        "manifest": "/artifacts/manifest.json",
        "barrier_identity_digest": "0" * 64,
        "target": "rollback",
        "envelope_digest": "0" * 64,
    }
    value["barrier_identity_digest"] = canonical_barrier_digest(value)
    value["envelope_digest"] = canonical_envelope_digest(value)
    return value


class UpgradeIdentityTests(unittest.TestCase):
    def test_exact_v9_canonical_digests_are_stable(self) -> None:
        value = envelope()
        self.assertEqual(
            "b677eeb69a6f99864d74b091fbe3568ed2f1273a6580fc54243218f1fa6a1c81",
            value["barrier_identity_digest"],
        )
        self.assertEqual(
            "d1374baad0069e56df81ab9220d58a76940dec67de1bc52507169f11f8d9ece6",
            value["envelope_digest"],
        )
        self.assertEqual(set(ENVELOPE_FIELDS), set(validate_envelope(value)))

    def test_barrier_tuple_excludes_envelope_only_fields_but_binds_v9_target(self) -> None:
        value = envelope()
        original = canonical_barrier_digest(value)
        self.assertEqual(original, canonical_barrier_digest({**value, "backend": "git"}))
        self.assertEqual(
            original,
            canonical_barrier_digest({**value, "destination": "/artifacts/other.sqlite"}),
        )
        self.assertNotEqual(original, canonical_barrier_digest({**value, "target": "new"}))

    def test_every_envelope_field_is_bound_and_unknown_fields_are_rejected(self) -> None:
        value = envelope()
        for field in ENVELOPE_FIELDS:
            changed = dict(value)
            changed[field] = 3 if field in {"schema_version", "state_revision"} else "tampered"
            with self.subTest(field=field), self.assertRaises(UpgradeIdentityError):
                validate_envelope(changed)
        with self.assertRaises(UpgradeIdentityError):
            validate_envelope({**value, "unexpected": True})

    def test_noncanonical_and_overlapping_paths_are_rejected(self) -> None:
        for field, path in (
            ("artifact_root", "/artifacts/../escape"),
            ("artifact_root", "//artifacts"),
            ("source", "relative.sqlite"),
            ("destination", "/outside/backup.sqlite"),
            ("manifest", "/artifacts/backup.sqlite/manifest.json"),
        ):
            changed = {**envelope(), field: path}
            changed["barrier_identity_digest"] = canonical_barrier_digest(changed)
            changed["envelope_digest"] = canonical_envelope_digest(changed)
            with self.subTest(field=field), self.assertRaises(UpgradeIdentityError):
                validate_envelope(changed)


if __name__ == "__main__":
    unittest.main()
