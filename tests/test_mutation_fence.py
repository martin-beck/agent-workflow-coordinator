# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Focused tests for the provisioned authority-lock foundation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.mutation_fence import MutationFence, MutationFenceError, provision


class MutationFenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.root.chmod(0o700)
        self.authority = self.root / "coordinator.sqlite3"
        self.authority.write_bytes(b"existing authority")
        self.authority.chmod(0o600)
        self.marker = self.root / "upgrade-control-required.json"
        self.lifecycle = self.root / "wal-lifecycle.json"
        self.lock = self.root / "authority.lock"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_provision_binds_existing_authority_and_lock(self) -> None:
        record = provision(
            self.authority, self.marker, self.lifecycle, self.lock, "project-public-id"
        )
        self.assertEqual("project-public-id", record["project_id"])
        self.assertEqual("clean_checkpointed", json.loads(self.lifecycle.read_text())["state"])
        with MutationFence(self.authority, self.marker, self.lifecycle, self.lock).locked():
            self.assertTrue(self.lock.exists())

    def test_unprovisioned_marker_is_rejected_by_seam(self) -> None:
        fence = MutationFence(self.authority, self.marker, self.lifecycle, self.lock)
        with self.assertRaisesRegex(MutationFenceError, "marker|missing"), fence.locked():
            pass
        self.assertTrue(self.authority.exists())

    def test_marker_tamper_and_authority_replacement_fail_closed(self) -> None:
        provision(self.authority, self.marker, self.lifecycle, self.lock, "project-public-id")
        replacement = self.root / "replacement"
        replacement.write_bytes(b"replacement")
        replacement.chmod(0o600)
        self.authority.unlink()
        replacement.rename(self.authority)
        with self.assertRaisesRegex(MutationFenceError, "identity"):
            MutationFence(self.authority, self.marker, self.lifecycle, self.lock)._verify()

        self.authority.unlink()
        self.authority.write_bytes(b"existing authority")
        self.authority.chmod(0o600)
        marker = json.loads(self.marker.read_text())
        marker["project_id"] = "other"
        self.marker.write_text(json.dumps(marker) + "\n")
        with self.assertRaisesRegex(MutationFenceError, "digest"):
            MutationFence(self.authority, self.marker, self.lifecycle, self.lock)._verify()

    def test_lock_replacement_fails_closed(self) -> None:
        provision(self.authority, self.marker, self.lifecycle, self.lock, "project-public-id")
        replacement = self.root / "replacement-lock"
        replacement.write_bytes(b"")
        replacement.chmod(0o600)
        self.lock.unlink()
        replacement.rename(self.lock)
        with self.assertRaisesRegex(MutationFenceError, "authority.lock identity"):
            MutationFence(self.authority, self.marker, self.lifecycle, self.lock)._verify()

    def test_malformed_lifecycle_is_fail_closed(self) -> None:
        provision(self.authority, self.marker, self.lifecycle, self.lock, "project-public-id")
        self.lifecycle.write_text("not-json\n")
        with self.assertRaisesRegex(MutationFenceError, "lifecycle is unreadable"):
            MutationFence(self.authority, self.marker, self.lifecycle, self.lock)._verify()

    def test_lifecycle_digest_and_project_id_are_validated(self) -> None:
        provision(self.authority, self.marker, self.lifecycle, self.lock, "project-public-id")
        lifecycle = json.loads(self.lifecycle.read_text())
        lifecycle["state"] = "active"
        self.lifecycle.write_text(json.dumps(lifecycle) + "\n")
        with self.assertRaisesRegex(MutationFenceError, "lifecycle digest"):
            MutationFence(self.authority, self.marker, self.lifecycle, self.lock)._verify()
        with self.assertRaisesRegex(MutationFenceError, "canonical and opaque"):
            provision(
                self.authority,
                self.root / "marker-2",
                self.root / "life-2",
                self.root / "lock-2",
                "path/id",
            )

    def test_marker_and_lifecycle_symlinks_are_rejected(self) -> None:
        provision(self.authority, self.marker, self.lifecycle, self.lock, "project-public-id")
        marker_copy = self.root / "marker-copy"
        marker_copy.write_bytes(self.marker.read_bytes())
        marker_copy.chmod(0o600)
        self.marker.unlink()
        self.marker.symlink_to(marker_copy)
        with self.assertRaisesRegex(MutationFenceError, "unreadable|descriptor"):
            MutationFence(self.authority, self.marker, self.lifecycle, self.lock)._verify()

        self.marker.unlink()
        self.marker_copy = self.root / "marker-copy-2"
        self.marker_copy.write_bytes(marker_copy.read_bytes())
        self.marker_copy.chmod(0o600)
        self.marker_copy.rename(self.marker)
        lifecycle_copy = self.root / "lifecycle-copy"
        lifecycle_copy.write_bytes(self.lifecycle.read_bytes())
        lifecycle_copy.chmod(0o600)
        self.lifecycle.unlink()
        self.lifecycle.symlink_to(lifecycle_copy)
        with self.assertRaisesRegex(MutationFenceError, "unreadable|descriptor"):
            MutationFence(self.authority, self.marker, self.lifecycle, self.lock)._verify()


if __name__ == "__main__":
    unittest.main()
