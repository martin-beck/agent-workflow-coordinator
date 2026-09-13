# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Focused tests for bounded TLC admission and resource containment."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_runner_spec = importlib.util.spec_from_file_location("tlc_runner", ROOT / "tools/tlc_runner.py")
assert _runner_spec and _runner_spec.loader
_runner = importlib.util.module_from_spec(_runner_spec)
_runner_spec.loader.exec_module(_runner)
AdmissionError = _runner.AdmissionError
_prune_stale = _runner._prune_stale
build_command = _runner.build_command
run = _runner.run


class TLCAdmissionTests(unittest.TestCase):
    def test_command_has_finite_workers_heap_and_cgroup_limits(self) -> None:
        command = build_command(
            jar=Path("tla.jar"),
            model=Path("Model.tla"),
            config=Path("Model.cfg"),
            metadir=Path("states"),
        )
        self.assertIn("-workers", command)
        self.assertIn("2", command)
        self.assertIn("-Xmx2048m", command)
        self.assertIn("--property=MemoryMax=3G", command)
        self.assertIn("--property=MemorySwapMax=3G", command)
        self.assertIn("--property=CPUQuota=200%", command)
        self.assertIn("--property=TasksMax=64", command)

    def test_uncontained_execution_is_explicit_only(self) -> None:
        command = build_command(
            jar=Path("tla.jar"),
            model=Path("Model.tla"),
            config=Path("Model.cfg"),
            metadir=Path("states"),
            cgroup_mode="off",
        )
        self.assertEqual(command[0], "java")
        self.assertNotIn("auto", command)

    def test_rejects_heap_that_exceeds_memory(self) -> None:
        with self.assertRaises(AdmissionError):
            build_command(
                jar=Path("tla.jar"),
                model=Path("Model.tla"),
                config=Path("Model.cfg"),
                metadir=Path("states"),
                heap="3G",
                memory_max="3G",
                cgroup_mode="off",
            )

    def test_required_cgroup_rejects_missing_systemd(self) -> None:
        with (
            patch("tools.tlc_runner.shutil.which", return_value=None),
            self.assertRaises(AdmissionError),
        ):
            build_command(
                jar=Path("tla.jar"),
                model=Path("Model.tla"),
                config=Path("Model.cfg"),
                metadir=Path("states"),
            )

    def test_queue_record_and_cleanup_survive_normal_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory)
            args = type(
                "Args",
                (),
                {
                    "queue": str(queue),
                    "model": "Model.tla",
                    "jar": "tla.jar",
                    "config": "Model.cfg",
                    "metadir": str(queue / "states"),
                    "workers": 2,
                    "heap": "64m",
                    "memory_max": "1G",
                    "swap_max": "1G",
                    "cgroup_mode": "off",
                },
            )()
            with patch("tools.tlc_runner.subprocess.run") as execute:
                execute.return_value.returncode = 0
                self.assertEqual(run(args), 0)
                execute.assert_called_once()
            self.assertEqual(list(queue.glob("*.json")), [])

    def test_stale_queue_records_are_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / "stale.json"
            record.write_text(json.dumps({"state": "queued"}))
            record.touch()
            with patch("tools.tlc_runner.time.time", return_value=record.stat().st_mtime + 90000):
                _prune_stale(Path(directory))
            self.assertFalse(record.exists())


if __name__ == "__main__":
    unittest.main()
