# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile tests for the uncalled lock-domain identity contract."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import cast

from tools.handoffctl import CoordinatorLockGuard, locked
from tools.lock_domain import LockDomainContract, LockDomainError
from tools.mutation_fence import MutationFence, provision, provision_control_binding
from tools.rollback_control_store import SQLiteBarrierSessionStore, SQLiteRollbackControlStore

PROJECT = "11111111-1111-4111-8111-111111111111"


class LockDomainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.root.chmod(0o700)
        self.authority = self.root / "authority.sqlite"
        self.authority.write_bytes(b"authority")
        self.authority.chmod(0o600)
        self.control = self.root / "control.sqlite"
        self.store = SQLiteRollbackControlStore(self.control, PROJECT, self.authority)
        self.marker = self.root / "authority-marker.json"
        self.lifecycle = self.root / "authority-lifecycle.json"
        self.authority_lock = self.root / "authority.lock"
        self.control_binding = self.root / "control-binding.json"
        provision(self.authority, self.marker, self.lifecycle, self.authority_lock, PROJECT)
        provision_control_binding(
            self.control,
            self.control_binding,
            self.store.control_lock_path,
            PROJECT,
        )
        self.fence = MutationFence(
            self.authority,
            self.marker,
            self.lifecycle,
            self.authority_lock,
            self.control,
            self.control_binding,
            self.store.control_lock_path,
        )
        self.session = SQLiteBarrierSessionStore(self.store, lambda: "authority-1")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_capture_and_revalidation_are_read_only_and_stable(self) -> None:
        with locked() as guard:
            captured = LockDomainContract.capture(guard, self.session, self.fence)
            captured.assert_current(guard, self.session, self.fence)
            self.assertEqual(captured, LockDomainContract.capture(guard, self.session, self.fence))

    def test_split_control_lock_is_rejected_before_opening_it(self) -> None:
        split = MutationFence(
            self.authority,
            self.marker,
            self.lifecycle,
            self.authority_lock,
            self.control,
            self.control_binding,
            self.root / "different-control.lock",
        )
        with locked() as guard, self.assertRaisesRegex(LockDomainError, "control-lock"):
            LockDomainContract.capture(guard, self.session, split)

    def test_mismatched_authority_and_duplicate_path_are_rejected(self) -> None:
        other = self.root / "other-authority.sqlite"
        other.write_bytes(b"other")
        other.chmod(0o600)
        mismatched = MutationFence(
            other,
            self.marker,
            self.lifecycle,
            self.authority_lock,
            self.control,
            self.control_binding,
            self.store.control_lock_path,
        )
        with locked() as guard, self.assertRaisesRegex(LockDomainError, "authority path"):
            LockDomainContract.capture(guard, self.session, mismatched)

        duplicate = MutationFence(
            self.control,
            self.marker,
            self.lifecycle,
            self.control,
            self.control,
            self.control_binding,
            self.store.control_lock_path,
        )
        with locked() as guard, self.assertRaisesRegex(LockDomainError, "duplicated"):
            LockDomainContract.capture(guard, self.session, duplicate)

    def test_replaced_descriptor_and_inactive_guard_fail_closed(self) -> None:
        with locked() as guard:
            captured = LockDomainContract.capture(guard, self.session, self.fence)
        with self.assertRaisesRegex(LockDomainError, "guard"):
            LockDomainContract.capture(cast(CoordinatorLockGuard, None), self.session, self.fence)

        replacement = self.root / "replacement.lock"
        replacement.write_bytes(b"")
        replacement.chmod(0o600)
        self.store.control_lock_path.unlink()
        replacement.replace(self.store.control_lock_path)
        with locked() as guard, self.assertRaisesRegex(LockDomainError, "binding|identity"):
            captured.assert_current(guard, self.session, self.fence)

    def test_invalid_binding_json_is_rejected_and_no_state_is_written(self) -> None:
        before = self.marker.read_bytes()
        value = json.loads(before)
        value["project_id"] = "other"
        self.marker.write_text(json.dumps(value) + "\n")
        with locked() as guard, self.assertRaisesRegex(LockDomainError, "binding"):
            LockDomainContract.capture(guard, self.session, self.fence)
        self.assertNotEqual(before, self.marker.read_bytes())


if __name__ == "__main__":
    unittest.main()
