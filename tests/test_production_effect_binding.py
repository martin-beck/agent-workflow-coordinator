# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools.authority_neutral_commit import CommitAdmissionBundle
from tools.git_authority_adapter import GitAuthorityAdapter
from tools.production_effect_binding import (
    ProductionEffectBindingError,
    bind_durable_commit_capability,
    bind_durable_rollback_capability,
)
from tools.sqlite_authority_adapter import SQLiteAuthorityAdapter
from tools.upgrade_binding import LiveUpgradeBinding


def _binding(backend: str) -> LiveUpgradeBinding:
    binding = object.__new__(LiveUpgradeBinding)
    adapter = object.__new__(GitAuthorityAdapter if backend == "git" else SQLiteAuthorityAdapter)
    runtime = SimpleNamespace(
        runtime_envelope={
            "backend": backend,
            "operation_id": "production-effect-test",
            "state_revision": 3,
            "durable_barrier_id": "barrier-test",
            "fencing_token": "fence-test",
        }
    )
    for field, value in {
        "runtime": runtime,
        "adapter": adapter,
        "scope": object(),
        "lease": object(),
        "admission_recheck": object(),
        "expected_branch": "main" if backend == "git" else None,
        "expected_head": "a" * 40 if backend == "git" else None,
        "expected_git_repository": None,
        "session": object(),
        "_token": object(),
    }.items():
        object.__setattr__(binding, field, value)
    return binding


def _admission(backend: str, target: str = "new") -> CommitAdmissionBundle:
    return CommitAdmissionBundle(
        backend=backend,
        target=target,
        operation_id="production-effect-test:commit"
        if target == "new"
        else "production-effect-test:rollback",
        fencing_token="fence-test",  # noqa: S106
        state_revision=3,
        barrier_id="barrier-test",
        artifact_identity="artifact-test",
        manifest_identity="manifest-test",
        selector_identity="selector-test",
        runtime_identity="runtime-test",
    )


class ProductionEffectBindingTests(unittest.TestCase):
    def test_binds_sqlite_durable_commit_only_after_identity_validation(self) -> None:
        binding = _binding("sqlite")
        capability = object()
        adapter_method = Mock(return_value=capability)
        with (
            patch.object(LiveUpgradeBinding, "is_admitted", return_value=True),
            patch.object(SQLiteAuthorityAdapter, "bind_durable_commit_capability", adapter_method),
        ):
            result = bind_durable_commit_capability(
                binding,
                _admission("sqlite"),
                object(),
                session_revision=3,
                admission_reread=dict,
            )
        self.assertIs(result, capability)
        self.assertEqual(adapter_method.call_args.kwargs["session_revision"], 3)

    def test_binds_git_durable_commit_with_branch_and_head_fence(self) -> None:
        binding = _binding("git")
        adapter_method = Mock(return_value=object())
        runner = object()
        with (
            patch.object(LiveUpgradeBinding, "is_admitted", return_value=True),
            patch.object(GitAuthorityAdapter, "bind_durable_commit_capability", adapter_method),
        ):
            bind_durable_commit_capability(
                binding,
                _admission("git"),
                object(),
                session_revision=3,
                admission_reread=dict,
                runner=runner,
            )
        self.assertEqual(adapter_method.call_args.kwargs["expected_branch"], "main")
        self.assertEqual(adapter_method.call_args.kwargs["expected_head"], "a" * 40)
        self.assertIs(adapter_method.call_args.kwargs["runner"], runner)

    def test_rejects_stale_identity_before_adapter_call(self) -> None:
        binding = _binding("sqlite")
        foreign = CommitAdmissionBundle(
            backend="sqlite",
            target="new",
            operation_id="production-effect-test:commit",
            fencing_token="foreign-fence",  # noqa: S106
            state_revision=3,
            barrier_id="barrier-test",
            artifact_identity="artifact-test",
            manifest_identity="manifest-test",
            selector_identity="selector-test",
            runtime_identity="runtime-test",
        )
        with (
            patch.object(LiveUpgradeBinding, "is_admitted", return_value=True),
            patch.object(
                SQLiteAuthorityAdapter, "bind_durable_commit_capability"
            ) as adapter_method,
            self.assertRaises(ProductionEffectBindingError),
        ):
            bind_durable_commit_capability(
                binding,
                foreign,
                object(),
                session_revision=3,
                admission_reread=dict,
            )
        adapter_method.assert_not_called()

    def test_binds_rollback_effect_internally_for_both_concrete_backends(self) -> None:
        for backend, adapter_type in (
            ("git", GitAuthorityAdapter),
            ("sqlite", SQLiteAuthorityAdapter),
        ):
            binding = _binding(backend)
            adapter_method = Mock(return_value=object())
            with (
                patch.object(LiveUpgradeBinding, "is_admitted", return_value=True),
                patch.object(adapter_type, "bind_durable_rollback_capability", adapter_method),
            ):
                bind_durable_rollback_capability(
                    binding,
                    _admission(backend, "rollback"),
                    object(),
                    lambda _argument: {"ok": True},
                    session_revision=3,
                )
            adapter_method.assert_called_once()


if __name__ == "__main__":
    unittest.main()
