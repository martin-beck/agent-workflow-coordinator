# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Tests for the fail-closed dependency-free upgrade command boundary."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from tools.generate_upgrade_contract import generate
from tools.upgrade_commands import (
    MAX_CONTRACT_BYTES,
    UpgradeCommandError,
    execute_upgrade_command,
)
from tools.upgrade_contract_runtime import RuntimeContractError, validate_runtime_contract


def contract(backend: str = "sqlite") -> dict[str, Any]:
    def release(version: str, seed: str) -> dict[str, str]:
        return {
            "version": version,
            "source_commit": seed * 40,
            "tag_ref": f"refs/tags/{version}",
            "tag_object": chr(ord(seed) + 1) * 40,
            "signature_sha256": chr(ord(seed) + 2) * 64,
            "trust_policy_sha256": chr(ord(seed) + 3) * 64,
            "vendor_manifest_sha256": chr(ord(seed) + 4) * 64,
        }

    return generate(
        {
            "operation_id": "upgrade:v0.3.5-to-v0.3.6:001",
            "backend": backend,
            "selector_ref": ".runtime/runtime-selector.json",
            "expected_state_revision": 7,
            "barrier_id": "barrier-7",
            "fencing_token": "fence-7",
            "from": release("v0.3.5", "a"),
            "to": release("v0.3.6", "b"),
        }
    )


class UpgradeCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "contract.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, document: object) -> None:
        self.path.write_text(json.dumps(document), encoding="utf-8")

    def test_check_and_plan_emit_only_sanitized_non_executable_data(self) -> None:
        document = contract()
        self.write(document)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, execute_upgrade_command("check", self.path, "sqlite"))
        checked = json.loads(output.getvalue())
        self.assertTrue(checked["valid"])
        self.assertFalse(checked["executable"])
        self.assertNotIn("phases", checked)

        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, execute_upgrade_command("plan", self.path, "sqlite"))
        planned = json.loads(output.getvalue())
        self.assertEqual("authority.atomic_replace", planned["phases"][5]["opcode"])
        self.assertEqual("backend.restore", planned["rollback"]["opcode"])
        encoded = output.getvalue()
        for private_value in (
            ".runtime/runtime-selector.json",
            "barrier-7",
            "fence-7",
            document["from"]["source_commit"],
            document["to"]["signature_sha256"],
        ):
            self.assertNotIn(private_value, encoded)

    def test_apply_and_rollback_reject_both_backends_without_writes(self) -> None:
        for backend in ("git", "sqlite"):
            for action in ("apply", "rollback"):
                document = contract(backend)
                self.write(document)
                before = self.path.read_bytes()
                with (
                    self.subTest(backend=backend, action=action),
                    self.assertRaisesRegex(UpgradeCommandError, "no coordinator state was mutated"),
                ):
                    execute_upgrade_command(action, self.path, backend)
                self.assertEqual(before, self.path.read_bytes())
                self.assertEqual([self.path], list(self.root.iterdir()))

    def test_contract_file_and_dispatch_boundaries_fail_closed(self) -> None:
        self.write(contract())
        with self.assertRaisesRegex(UpgradeCommandError, "does not match"):
            execute_upgrade_command("check", self.path, "git")
        with self.assertRaisesRegex(UpgradeCommandError, "unsupported"):
            execute_upgrade_command("check", self.path, "remote")
        with self.assertRaisesRegex(UpgradeCommandError, "unknown upgrade action"):
            execute_upgrade_command("execute", self.path, "sqlite")

        self.path.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(UpgradeCommandError, "not canonical JSON"):
            execute_upgrade_command("check", self.path, "sqlite")
        self.path.write_text('{"schema_version":2,"schema_version":2}', encoding="utf-8")
        with self.assertRaisesRegex(UpgradeCommandError, "not canonical JSON"):
            execute_upgrade_command("check", self.path, "sqlite")
        self.path.write_text('{"schema_version":NaN}', encoding="utf-8")
        with self.assertRaisesRegex(UpgradeCommandError, "not canonical JSON"):
            execute_upgrade_command("check", self.path, "sqlite")
        self.write([])
        with self.assertRaisesRegex(UpgradeCommandError, "must be an object"):
            execute_upgrade_command("check", self.path, "sqlite")
        self.path.write_bytes(b"x" * (MAX_CONTRACT_BYTES + 1))
        with self.assertRaisesRegex(UpgradeCommandError, "size limit"):
            execute_upgrade_command("check", self.path, "sqlite")

    def test_contract_hardlink_alias_is_rejected_before_dispatch(self) -> None:
        target = self.root / "contract-target.json"
        target.write_text(json.dumps(contract()), encoding="utf-8")
        self.path.hardlink_to(target)
        with self.assertRaisesRegex(UpgradeCommandError, "single-link regular file"):
            execute_upgrade_command("check", self.path, "sqlite")
        self.path.unlink()
        target = self.root / "target.json"
        target.write_text("{}", encoding="utf-8")
        self.path.symlink_to(target)
        with self.assertRaisesRegex(UpgradeCommandError, "unavailable or unsafe"):
            execute_upgrade_command("check", self.path, "sqlite")

    def test_stdlib_validator_rejects_every_typed_contract_boundary(self) -> None:
        mutations: tuple[Callable[[dict[str, Any]], object], ...] = (
            lambda value: value.update(schema_version=True),
            lambda value: value.update(operation_id="bad/path"),
            lambda value: value.update(backend="remote"),
            lambda value: value.update(backend=[]),
            lambda value: value["from"].update(version="latest"),
            lambda value: value["from"].update(source_commit="short"),
            lambda value: value["from"].update(tag_object="short"),
            lambda value: value["from"].update(tag_ref="refs/tags/v9.9.9"),
            lambda value: value["from"].update(signature_sha256="short"),
            lambda value: value.update(to=value["from"].copy()),
            lambda value: value.update(preconditions=[]),
            lambda value: value["preconditions"][0].update(id="bad"),
            lambda value: value["preconditions"][0].update(effect="execute"),
            lambda value: value["preconditions"][0].update(effect=[]),
            lambda value: value["preconditions"][0].update(failure_mode="continue"),
            lambda value: value["preconditions"][0].update(evidence=[]),
            lambda value: value["preconditions"].append(value["preconditions"][0].copy()),
            lambda value: value.update(phases=[]),
            lambda value: value["phases"][0].update(id="stage"),
            lambda value: value["phases"][0].update(order=True),
            lambda value: value["phases"][0].update(mutates_authority=True),
            lambda value: value["phases"][0].update(requires=["reopen"]),
            lambda value: value["phases"][0].update(on_failure="continue"),
            lambda value: value["phases"][0].update(on_failure=[]),
            lambda value: value["phases"][0]["operation"].update(operation_id="unbound"),
            lambda value: value["phases"][0]["operation"].update(opcode="runtime.stage"),
            lambda value: value["phases"][0]["operation"].update(opcode=[]),
            lambda value: value["phases"][0]["operation"].update(timeout_seconds=False),
            lambda value: value["phases"][0]["operation"].update(resources=[]),
            lambda value: value["phases"][0]["operation"].update(durable_record="memory"),
            lambda value: value["phases"][0]["operation"].update(durable_record=[]),
            lambda value: value["phases"][0]["operation"]["inputs"].update(backend="git"),
            lambda value: value["phases"][0]["operation"]["inputs"].update(selector_ref="../x"),
            lambda value: value["phases"][0]["operation"]["inputs"].update(barrier_id="bad/x"),
            lambda value: value["phases"][0]["operation"]["inputs"].update(
                expected_state_revision=False
            ),
            lambda value: value["phases"][0]["operation"]["inputs"].update(
                backup_operation_id="unbound"
            ),
            lambda value: value["phases"][1]["operation"]["inputs"].__setitem__(
                "fencing_token", "changed"
            ),
            lambda value: value.update(backend_contracts=[]),
            lambda value: value["backend_contracts"][0].update(backend="remote"),
            lambda value: value["backend_contracts"][0].update(backend=[]),
            lambda value: value["backend_contracts"][0].update(authority=[]),
            lambda value: value["backend_contracts"][0].update(equivalence="none"),
            lambda value: value["backend_contracts"].__setitem__(
                1, value["backend_contracts"][0].copy()
            ),
            lambda value: value["rollback"].update(required=False),
            lambda value: value["rollback"].update(ambiguous_external_result=[]),
            lambda value: value["rollback"]["operation"].update(opcode="barrier.reopen"),
            lambda value: value["rollback"]["operation"]["inputs"].__setitem__(
                "fencing_token", "changed"
            ),
            lambda value: value.update(unexpected=True),
        )
        for mutate in mutations:
            document = contract()
            mutate(document)
            with self.subTest(document=document), self.assertRaises(RuntimeContractError):
                validate_runtime_contract(document)


if __name__ == "__main__":
    unittest.main()
