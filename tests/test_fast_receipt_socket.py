# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Negative and independent-process transport checks for opt-in receipts."""

from __future__ import annotations

import contextlib
import io
import json
import socket
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tools.fast_receipt_socket import (
    _client_common_dir,
    _fast_request,
    _safe_socket,
    socket_service,
    try_socket_fast,
)


class FakeBoundCore:
    def __init__(self, root: Path, project_id: str) -> None:
        self.root = root
        self.project_id = project_id
        self.reject_caller = False

    def coordinator_lock_path(self) -> Path:
        return self.root / "state.lock"

    def assert_project_binding(self, caller_cwd: Path | None = None) -> None:
        if self.reject_caller or caller_cwd != Path.cwd().resolve():
            raise RuntimeError("foreign fast receipt caller")

    def backend_selection(self) -> dict[str, str]:
        return {"backend": "git"}

    def project_binding(self) -> dict[str, str]:
        return {"project_id": self.project_id}


class FastReceiptSocketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.private = Path(self.directory.name) / "handoffctl"
        self.private.mkdir(mode=0o700)
        self.path = self.private / "fast-receipts.sock"
        self.core = FakeBoundCore(self.private, str(uuid.uuid4()))
        self.argv = [
            "fast", "heartbeat", "AR-0120", "--owner", "worker-a",
            "--expected-revision", "2", "--lease-minutes", "20",
            "--key", "worker-a:heartbeat:2",
        ]

    def test_canonical_request_and_malformed_args_fall_back_to_parser(self) -> None:
        request = _fast_request(self.argv)
        assert request is not None
        self.assertEqual("heartbeat", request["action"])
        self.assertEqual(2, request["expected_revision"])
        self.assertEqual(20, request["lease_minutes"])
        self.assertIsNone(_fast_request([*self.argv, "--owner", "duplicate"]))
        self.assertIsNone(_fast_request(["fast", "heartbeat", "AR-0120", "--owner"]))
        self.assertIsNone(_fast_request(["fast", "worker", "--serve"]))
        self.assertEqual(
            {"protocol": 1, "action": "receipt", "receipt_id": "a" * 32},
            _fast_request(["fast", "receipt", "a" * 32]),
        )

    def test_git_common_directory_resolves_main_and_linked_worktree(self) -> None:
        root = Path(self.directory.name)
        marker = root / ".git"
        marker.mkdir()
        self.assertEqual(marker.resolve(), _client_common_dir(root))
        marker.rmdir()
        gitdir = root / "separate" / "worktrees" / "worker"
        gitdir.mkdir(parents=True)
        (gitdir / "commondir").write_text("../..\n")
        marker.write_text(f"gitdir: {gitdir}\n")
        self.assertEqual((root / "separate").resolve(), _client_common_dir(root))
        marker.write_text("not a gitdir")
        self.assertIsNone(_client_common_dir(root))

    def test_unsafe_socket_symlink_is_not_followed(self) -> None:
        target = self.private / "target"
        target.write_text("not a socket")
        self.path.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            _safe_socket(self.path)
        with self.assertRaisesRegex(RuntimeError, "unsafe"), socket_service(self.core):
            pass
        self.assertTrue(self.path.is_symlink())

    def test_bound_service_returns_only_durable_queued_and_read_receipts(self) -> None:
        with socket_service(self.core), patch(
            "tools.fast_receipt_socket._socket_path", return_value=self.path
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(0, try_socket_fast(self.argv))
            queued = json.loads(output.getvalue())
            self.assertEqual("queued-local", queued["phase"])
            self.assertIsNone(queued["commit_oid"])
            receipt_id = queued["receipt_id"]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(0, try_socket_fast(["fast", "receipt", receipt_id]))
            self.assertEqual(queued, json.loads(output.getvalue()))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, try_socket_fast(self.argv))
            self.assertEqual(
                1,
                self._intent_count(),
            )
        self.assertFalse(self.path.exists())

    def _intent_count(self) -> int:
        import sqlite3

        with contextlib.closing(
            sqlite3.connect(self.private / "fast-receipts.sqlite3")
        ) as connection:
            return int(connection.execute("SELECT count(*) FROM intents").fetchone()[0])

    def test_foreign_caller_rejects_before_queue_effect(self) -> None:
        self.core.reject_caller = True
        with socket_service(self.core), patch(
            "tools.fast_receipt_socket._socket_path", return_value=self.path
        ):
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                self.assertEqual(1, try_socket_fast(self.argv))
            self.assertIn("foreign fast receipt caller", error.getvalue())
        self.assertFalse((self.private / "fast-receipts.sqlite3").exists())

    def test_stale_owned_socket_is_replaced_but_unsafe_socket_fails(self) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
            stale.bind(str(self.path))
            self.path.chmod(0o600)
        with socket_service(self.core):
            self.assertTrue(_safe_socket(self.path))
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
