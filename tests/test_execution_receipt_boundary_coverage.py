# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Focused receipt and descriptor-bound execution-envelope boundary tests."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools import execution_envelope
from tools.execution_receipt import build, matches, valid


def reservation() -> dict[str, object]:
    """Return one complete reservation identity used by terminal receipts."""
    return {
        "execution_id": "a" * 32,
        "owner": "worker-a",
        "envelope_digest": "sha256:" + "b" * 64,
        "argv_digest": "c" * 64,
    }


def receipt(**overrides: object) -> dict[str, object]:
    """Return one valid terminal receipt, allowing one focused mutation."""
    result = {
        **reservation(),
        "classification": "EXIT",
        "returncode": 0,
        "recorded_at": "2099-01-01T00:00:00+00:00",
    }
    result.update(overrides)
    return result


class ExecutionReceiptBoundaryCoverageTests(unittest.TestCase):
    """Keep receipt construction bound to the exact durable execution identity."""

    def test_valid_receipts_require_closed_shape_types_and_aware_timestamp(self) -> None:
        self.assertTrue(valid(receipt()))
        cases = (
            receipt(extra="forged"),
            receipt(returncode=True),
            receipt(recorded_at="2099-01-01T00:00:00"),
            receipt(classification="TIMEOUT"),
            receipt(argv_digest="bad"),
        )
        for invalid_receipt in cases:
            self.assertFalse(valid(invalid_receipt))

    def test_matches_requires_valid_exact_execution_identity(self) -> None:
        bound = reservation()
        self.assertTrue(matches(bound, receipt()))
        for field in ("execution_id", "owner", "envelope_digest", "argv_digest"):
            altered = receipt(**{field: "different"})
            self.assertFalse(matches(bound, altered), field)
        self.assertFalse(matches(bound, {"not": "a receipt"}))

    def test_build_rejects_identity_mismatch_and_invalid_terminal_result(self) -> None:
        bound = reservation()
        arguments = argparse.Namespace(
            execution_id=bound["execution_id"],
            owner=bound["owner"],
            classification="EXIT",
            returncode=3,
            recorded_at="2099-01-01T00:00:00Z",
        )
        self.assertEqual(
            receipt(returncode=3, recorded_at=arguments.recorded_at), build(bound, arguments)
        )
        arguments.execution_id = "d" * 32
        with self.assertRaisesRegex(ValueError, "no longer matches"):
            build(bound, arguments)
        arguments.execution_id = bound["execution_id"]
        arguments.owner = "worker-b"
        with self.assertRaisesRegex(ValueError, "owner does not match"):
            build(bound, arguments)
        arguments.owner = bound["owner"]
        arguments.returncode = True
        with self.assertRaisesRegex(ValueError, "invalid execution result"):
            build(bound, arguments)


class ExecutionEnvelopeDescriptorBoundaryCoverageTests(unittest.TestCase):
    """Exercise link-free descriptor opening and cleanup without launching Bubblewrap."""

    def test_open_path_requires_absolute_path_and_rejects_non_directory_components(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            leaf = root / "leaf"
            leaf.write_text("not a directory", encoding="utf-8")
            descriptor = execution_envelope._open_path(root, directory=True)
            try:
                self.assertEqual(root, Path(f"/proc/self/fd/{descriptor}").readlink())
            finally:
                os.close(descriptor)
            with self.assertRaisesRegex(RuntimeError, "must be absolute"):
                execution_envelope._open_path(Path("relative"))
            with self.assertRaisesRegex(RuntimeError, "non-directory component"):
                execution_envelope._open_path(leaf / "child")
            with self.assertRaisesRegex(RuntimeError, "not a directory"):
                execution_envelope._open_path(leaf, directory=True)

    def test_scope_opening_rejects_unsafe_or_linked_path_and_accepts_relative_directory(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source").mkdir()
            (root / "outside").mkdir()
            (root / "source" / "link").symlink_to("../outside")
            descriptor = execution_envelope._open_scope_path(root, "source")
            try:
                self.assertEqual(root / "source", Path(f"/proc/self/fd/{descriptor}").readlink())
            finally:
                os.close(descriptor)
            for unsafe in ("", ".", "../outside"):
                with self.assertRaisesRegex(RuntimeError, "path is unsafe"):
                    execution_envelope._open_scope_path(root, unsafe)
            with self.assertRaisesRegex(RuntimeError, "non-directory component"):
                execution_envelope._open_scope_path(root, "source/link/child")

    def test_sandbox_helper_closes_every_opened_descriptor_when_exec_fails(self) -> None:
        payload = {
            "command": ["bwrap", "--tmpfs", "/"],
            "specification": {
                "root": "/worktree-root",
                "mounts": [
                    {"relative": "read", "destination": "/worktree/read", "mode": "--ro-bind"},
                    {"relative": "write", "destination": "/worktree/write", "mode": "--bind"},
                ],
            },
        }
        with (
            patch.object(
                sys,
                "argv",
                ["helper", "/source", json.dumps(payload), "--", "true"],
            ),
            patch.object(
                execution_envelope,
                "_open_path",
                side_effect=[10, FileNotFoundError(), 11],
            ),
            patch.object(execution_envelope, "_open_scope_path", side_effect=[12, 13]),
            patch.object(shutil, "which", return_value="/usr/bin/bwrap"),
            patch.object(os, "close") as close,
            patch.object(os, "execvpe", side_effect=OSError("exec failed")),
            self.assertRaisesRegex(OSError, "exec failed"),
        ):
            execution_envelope._run_sandbox_helper()
        self.assertCountEqual([10, 11, 12, 13], [call.args[0] for call in close.call_args_list])
