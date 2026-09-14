# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Focused tests for the provisioned authority-lock foundation."""

from __future__ import annotations

import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools import mutation_fence
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
        marker_copy = self.root / "marker-copy-2"
        marker_copy.write_bytes(self.root.joinpath("marker-copy").read_bytes())
        marker_copy.chmod(0o600)
        marker_copy.rename(self.marker)
        lifecycle_copy = self.root / "lifecycle-copy"
        lifecycle_copy.write_bytes(self.lifecycle.read_bytes())
        lifecycle_copy.chmod(0o600)
        self.lifecycle.unlink()
        self.lifecycle.symlink_to(lifecycle_copy)
        with self.assertRaisesRegex(MutationFenceError, "unreadable|descriptor"):
            MutationFence(self.authority, self.marker, self.lifecycle, self.lock)._verify()

    def test_invalid_and_missing_paths_fail_closed(self) -> None:
        with self.assertRaisesRegex(MutationFenceError, "canonical"):
            provision(
                Path("relative.db"), self.marker, self.lifecycle, self.lock, "project-public-id"
            )
        with self.assertRaisesRegex(MutationFenceError, "missing"):
            provision(
                self.root / "missing.db",
                self.marker,
                self.lifecycle,
                self.lock,
                "project-public-id",
            )
        parent = self.root / "parent"
        parent.mkdir(mode=0o700)
        target = parent / "authority"
        target.write_bytes(b"authority")
        target.chmod(0o600)
        parent.chmod(0o755)
        with self.assertRaisesRegex(MutationFenceError, "owner-only"):
            provision(target, parent / "marker", parent / "lifecycle", parent / "lock", "project")

    def test_authority_and_lock_permissions_are_rejected(self) -> None:
        self.authority.chmod(0o644)
        with self.assertRaisesRegex(MutationFenceError, "owner-only"):
            provision(self.authority, self.marker, self.lifecycle, self.lock, "project")
        self.authority.chmod(0o600)
        self.lock.write_bytes(b"")
        self.lock.chmod(0o644)
        with self.assertRaisesRegex(MutationFenceError, "permissions"):
            provision(self.authority, self.marker, self.lifecycle, self.lock, "project")

    def test_existing_provisioning_is_idempotent_and_mismatch_rejected(self) -> None:
        first = provision(self.authority, self.marker, self.lifecycle, self.lock, "project")
        self.assertEqual(
            first, provision(self.authority, self.marker, self.lifecycle, self.lock, "project")
        )
        lifecycle = json.loads(self.lifecycle.read_text())
        lifecycle["generation"] = 1
        self.lifecycle.write_text(json.dumps(lifecycle) + "\n")
        with self.assertRaisesRegex(MutationFenceError, "already provisioned"):
            provision(self.authority, self.marker, self.lifecycle, self.lock, "project")

    def test_marker_and_lifecycle_schema_and_state_fail_closed(self) -> None:
        provision(self.authority, self.marker, self.lifecycle, self.lock, "project")
        self.marker.write_text("[]\n")
        self.marker.chmod(0o600)
        with self.assertRaisesRegex(MutationFenceError, "schema"):
            MutationFence(self.authority, self.marker, self.lifecycle, self.lock)._verify()
        self.marker.unlink()
        provision(self.authority, self.marker, self.lifecycle, self.lock, "project")
        lifecycle = json.loads(self.lifecycle.read_text())
        lifecycle["state"] = "bad"
        lifecycle["record_digest"] = mutation_fence._digest(
            {key: value for key, value in lifecycle.items() if key != "record_digest"}
        )
        self.lifecycle.write_text(json.dumps(lifecycle) + "\n")
        with self.assertRaisesRegex(MutationFenceError, "state"):
            MutationFence(self.authority, self.marker, self.lifecycle, self.lock)._verify()

    def test_marker_and_publication_failures_are_ambiguous(self) -> None:
        marker = self.root / "marker-existing"
        marker.write_text("{}\n")
        marker.chmod(0o600)
        with self.assertRaisesRegex(MutationFenceError, "another identity"):
            provision(self.authority, marker, self.lifecycle, self.lock, "project")
        with (
            patch.object(os, "rename", side_effect=OSError("rename failed")),
            self.assertRaisesRegex(MutationFenceError, "ambiguous"),
        ):
            provision(
                self.authority,
                self.root / "marker-new",
                self.root / "lifecycle-new",
                self.root / "lock-new",
                "project",
            )

    def test_lock_acquisition_errors_are_fail_closed(self) -> None:
        provision(self.authority, self.marker, self.lifecycle, self.lock, "project")
        with (
            patch(
                "tools.mutation_fence.fcntl.flock",
                side_effect=[OSError(errno.EACCES, "denied"), None],
            ),
            self.assertRaisesRegex(MutationFenceError, "acquisition"),
            MutationFence(self.authority, self.marker, self.lifecycle, self.lock).locked(),
        ):
            pass

    def test_record_permissions_and_descriptor_races_fail_closed(self) -> None:
        provision(self.authority, self.marker, self.lifecycle, self.lock, "project")
        self.marker.chmod(0o644)
        with self.assertRaisesRegex(MutationFenceError, "owner-only"):
            MutationFence(self.authority, self.marker, self.lifecycle, self.lock)._verify()
        self.marker.chmod(0o600)
        parent_status = self.root.stat()
        file_status = self.marker.stat()
        changed_status = self.authority.stat()
        with (
            patch.object(
                os, "fstat", side_effect=[parent_status, file_status, changed_status, parent_status]
            ),
            self.assertRaisesRegex(MutationFenceError, "identity changed"),
        ):
            mutation_fence._read_json(self.marker, "marker")
        changed_parent = SimpleNamespace(
            st_dev=parent_status.st_dev, st_ino=parent_status.st_ino + 1
        )
        with (
            patch.object(
                os,
                "fstat",
                side_effect=[
                    parent_status,
                    file_status,
                    file_status,
                    changed_parent,
                    changed_parent,
                ],
            ),
            self.assertRaisesRegex(MutationFenceError, "parent identity changed"),
        ):
            mutation_fence._read_json(self.marker, "marker")

    def test_path_lock_and_publication_failure_branches(self) -> None:
        symlink = self.root / "authority-link"
        symlink.symlink_to(self.authority)
        with self.assertRaisesRegex(MutationFenceError, "descriptor"):
            provision(symlink, self.marker, self.lifecycle, self.lock, "project")
        lock_link = self.root / "lock-link"
        lock_link.symlink_to(self.authority)
        with self.assertRaisesRegex(MutationFenceError, "cannot be provisioned"):
            provision(self.authority, self.marker, self.lifecycle, lock_link, "project")
        with (
            patch.object(os, "fsync", side_effect=[None, None, OSError("fsync failed")]),
            self.assertRaisesRegex(MutationFenceError, "ambiguous"),
        ):
            provision(
                self.authority,
                self.root / "marker-fsync",
                self.root / "lifecycle-fsync",
                self.root / "lock-fsync",
                "project",
            )

    def test_marker_schema_identity_and_lifecycle_binding_fail_closed(self) -> None:
        provision(self.authority, self.marker, self.lifecycle, self.lock, "project")
        marker = json.loads(self.marker.read_text())
        marker["schema_version"] = 2
        marker["identity_digest"] = mutation_fence._digest(
            {key: value for key, value in marker.items() if key != "identity_digest"}
        )
        self.marker.write_text(json.dumps(marker) + "\n")
        with self.assertRaisesRegex(MutationFenceError, "unsupported"):
            MutationFence(self.authority, self.marker, self.lifecycle, self.lock)._verify()
        marker["schema_version"] = 1
        marker["project_id"] = "bad/id"
        marker["identity_digest"] = mutation_fence._digest(
            {key: value for key, value in marker.items() if key != "identity_digest"}
        )
        self.marker.write_text(json.dumps(marker) + "\n")
        with self.assertRaisesRegex(MutationFenceError, "canonical"):
            MutationFence(self.authority, self.marker, self.lifecycle, self.lock)._verify()

        self.marker.unlink()
        provision(self.authority, self.marker, self.lifecycle, self.lock, "project")
        lifecycle = json.loads(self.lifecycle.read_text())
        lifecycle["authority_digest"] = "sha256:" + "0" * 64
        lifecycle["record_digest"] = mutation_fence._digest(
            {key: value for key, value in lifecycle.items() if key != "record_digest"}
        )
        self.lifecycle.write_text(json.dumps(lifecycle) + "\n")
        with self.assertRaisesRegex(MutationFenceError, "binding"):
            MutationFence(self.authority, self.marker, self.lifecycle, self.lock)._verify()

    def test_lock_identity_and_unexpected_lock_errors_fail_closed(self) -> None:
        provision(self.authority, self.marker, self.lifecycle, self.lock, "project")
        record = json.loads(self.marker.read_text())
        with (
            patch.object(MutationFence, "_verify", return_value=record),
            patch.object(mutation_fence, "_read_json", return_value=record),
            patch.object(
                mutation_fence,
                "_identity",
                return_value=mutation_fence.DescriptorIdentity(0, 0, 0, 0, 0, 0, 0),
            ),
            self.assertRaisesRegex(MutationFenceError, "identity changed"),
            MutationFence(self.authority, self.marker, self.lifecycle, self.lock).locked(),
        ):
            pass
        with (
            patch(
                "tools.mutation_fence.fcntl.flock",
                side_effect=[OSError(errno.EIO, "io"), None],
            ),
            self.assertRaises(OSError),
            MutationFence(self.authority, self.marker, self.lifecycle, self.lock).locked(),
        ):
            pass


if __name__ == "__main__":
    unittest.main()
