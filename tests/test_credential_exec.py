# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Offline contract tests for the ephemeral credential helper."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from tools import credential_exec


class CredentialExecTests(unittest.TestCase):
    def test_rejects_missing_command_without_reading_credentials(self) -> None:
        with self.assertRaises(SystemExit):
            credential_exec.main(["--credential-fd", "9"])

    def test_logs_in_from_fd_then_execs_without_argv_token(self) -> None:
        with tempfile.TemporaryFile() as stream:
            stream.write(b"ephemeral-token\n")
            stream.seek(0)
            descriptor = os.dup(stream.fileno())
            try:
                completed = type("Completed", (), {"returncode": 0})()
                with (
                    patch("tools.credential_exec.shutil.which", return_value="/usr/bin/gh"),
                    patch("tools.credential_exec.subprocess.run", return_value=completed) as run,
                    patch(
                        "tools.credential_exec.os.execvpe", side_effect=RuntimeError("exec")
                    ) as execvpe,
                    self.assertRaisesRegex(RuntimeError, "exec"),
                ):
                    credential_exec.main(
                        ["--credential-fd", str(descriptor), "gh", "pr", "merge", "171"]
                    )
                self.assertEqual(
                    ["/usr/bin/gh", "auth", "login", "--with-token"], run.call_args.args[0]
                )
                self.assertEqual("gh", execvpe.call_args.args[0])
                self.assertEqual(["gh", "pr", "merge", "171"], execvpe.call_args.args[1])
                self.assertNotIn("ephemeral-token", repr(execvpe.call_args.args))
            finally:
                pass

    def test_nonzero_login_is_returned_without_exec(self) -> None:
        with tempfile.TemporaryFile() as stream:
            descriptor = os.dup(stream.fileno())
            try:
                completed = type("Completed", (), {"returncode": 1})()
                with (
                    patch("tools.credential_exec.shutil.which", return_value="/usr/bin/gh"),
                    patch("tools.credential_exec.subprocess.run", return_value=completed),
                    patch("tools.credential_exec.os.execvpe") as execvpe,
                ):
                    self.assertEqual(
                        1,
                        credential_exec.main(["--credential-fd", str(descriptor), "gh", "pr"]),
                    )
                execvpe.assert_not_called()
            finally:
                os.close(descriptor)
