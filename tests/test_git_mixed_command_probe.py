# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Safety checks for 16-lane Git-backed mixed-route classification."""

import unittest

from git_mixed_command_probe import adversarial_commands, error_class, route_name


class GitMixedCommandProbeTests(unittest.TestCase):
    def test_route_names_keep_semantically_distinct_variants(self) -> None:
        self.assertEqual("doctor-live", route_name(["doctor", "--live"]))
        self.assertEqual("doctor", route_name(["doctor"]))
        self.assertEqual("render-check", route_name(["render-status", "--check"]))
        self.assertEqual("roles-assign", route_name(["roles", "assign"]))

    def test_expected_rejections_are_not_success(self) -> None:
        self.assertEqual("lock_timeout", error_class(b"LOCK_TIMEOUT"))
        self.assertEqual("wrong_owner", error_class(b"AR-9000 is owned by bench-0"))
        self.assertEqual("wrong_owner", error_class(b"task claim does not match owner"))
        self.assertEqual("stale_task_revision", error_class(b"stale revision: expected 2"))
        self.assertEqual("malformed_gate_artifact", error_class(b"--before must use REF=DIGEST"))
        self.assertEqual("stale_role_revision", error_class(b"stale role revision"))
        self.assertEqual(
            "unsupported_git_route", error_class(b"board requires the SQLite authority")
        )
        self.assertEqual("usage_error", error_class(b"usage: handoffctl"))

    def test_adversarial_batch_has_one_legitimate_worker(self) -> None:
        commands = adversarial_commands(4)
        self.assertEqual(16, len(commands))
        self.assertEqual(
            ["update", "AR-9000", "--owner", "bench-0", "--expected-revision", "4"],
            commands[-1][:6],
        )
        self.assertEqual(15, sum(command != commands[-1] for command in commands))


if __name__ == "__main__":
    unittest.main()
