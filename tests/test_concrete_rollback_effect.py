# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

# Fixed Git test executable and arguments are intentional fixtures.
# ruff: noqa: S603, S607

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from tools.authority_neutral_commit import CommitAdmissionBundle
from tools.git_authority_adapter import GitAuthorityAdapter, GitAuthorityError
from tools.production_rollback_effect import (
    ProductionRollbackEffectError,
    bind_concrete_durable_rollback_capability,
)
from tools.rollback_control_store import SQLiteBarrierSessionStore, SQLiteRollbackControlStore
from tools.sqlite_authority_adapter import SQLiteAuthorityAdapter, SQLiteAuthorityError
from tools.upgrade_binding import LiveUpgradeBinding
from tools.upgrade_identity import (
    BarrierChildIdentity,
    BarrierSessionIdentity,
    canonical_barrier_session_digest,
)

BINDING = {
    "project_id": "11111111-1111-4111-8111-111111111111",
    "state_repository": "owner/state",
    "product_repository": "owner/product",
}


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _rollback_admission(
    revision: int = 2,
    *,
    artifact_identity: str = "artifact-rollback",
    manifest_identity: str = "manifest-rollback",
) -> CommitAdmissionBundle:
    return CommitAdmissionBundle(
        backend="git",
        target="rollback",
        operation_id="real-git-rollback:rollback",
        fencing_token="fence-rollback",  # noqa: S106
        state_revision=revision,
        barrier_id="barrier-rollback",
        artifact_identity=artifact_identity,
        manifest_identity=manifest_identity,
        selector_identity="selector-rollback",
        runtime_identity="runtime-rollback",
    )


def _kill_after_git_rollback(
    control_path: str, project: str, repository: str, backup: str, forward_head: str
) -> None:
    journal = SQLiteBarrierSessionStore(
        SQLiteRollbackControlStore(Path(control_path), project),
        lambda: "authority-rollback",
    )
    adapter = GitAuthorityAdapter(Path(repository))
    manifest = json.loads((Path(backup) / "manifest.json").read_text(encoding="utf-8"))
    admission = _rollback_admission(
        artifact_identity=cast(str, manifest["commit"]),
        manifest_identity=hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    original = adapter.restore_authority_bound

    def kill_restore(*args: object, **kwargs: object) -> dict[str, object]:
        result = original(*args, **kwargs)
        os.kill(os.getpid(), signal.SIGKILL)
        return result

    with patch.object(adapter, "restore_authority_bound", side_effect=kill_restore):
        with patch.object(LiveUpgradeBinding, "is_admitted", return_value=True):
            capability = bind_concrete_durable_rollback_capability(
                _factory_binding(adapter, "git", "real-git-rollback"),
                admission,
                journal,
                Path(backup),
                session_revision=2,
                expected_branch="main",
                expected_head=forward_head,
            )
        capability.execute(None)


def _kill_after_sqlite_rollback(
    control_path: str,
    project: str,
    authority: str,
    backup: str,
    manifest: dict[str, object],
) -> None:
    journal = SQLiteBarrierSessionStore(
        SQLiteRollbackControlStore(Path(control_path), project),
        lambda: "authority-rollback",
    )
    adapter = SQLiteAuthorityAdapter(Path(authority))
    admission = CommitAdmissionBundle(
        backend="sqlite",
        target="rollback",
        operation_id="real-sqlite-rollback:rollback",
        fencing_token="fence-rollback",  # noqa: S106
        state_revision=2,
        barrier_id="barrier-rollback",
        artifact_identity=cast(str, manifest["database_sha256"]),
        manifest_identity=hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        selector_identity="selector-rollback",
        runtime_identity="runtime-rollback",
    )

    original = adapter.restore_bound

    def kill_restore(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)
        os.kill(os.getpid(), signal.SIGKILL)

    with patch.object(adapter, "restore_bound", side_effect=kill_restore):
        with patch.object(LiveUpgradeBinding, "is_admitted", return_value=True):
            capability = bind_concrete_durable_rollback_capability(
                _factory_binding(adapter, "sqlite", "real-sqlite-rollback"),
                admission,
                journal,
                Path(backup),
                session_revision=2,
                sqlite_manifest=manifest,
                sqlite_binding=BINDING,
            )
        capability.execute(None)


def _seed_rollback_session(control_path: Path, project: str) -> None:
    record: dict[str, object] = {
        "schema_version": 1,
        "project_id": project,
        "attempt_id": "rollback-attempt",
        "state_revision": 1,
        "authority_revision_at_acquire": "authority-rollback",
        "durable_barrier_id": "barrier-rollback",
        "fencing_token": "fence-rollback",
        "fencing_owner": "owner-rollback",
        "identity_digest": "0" * 64,
    }
    record["identity_digest"] = canonical_barrier_session_digest(record)
    identity = BarrierSessionIdentity.from_record(record)
    control = SQLiteRollbackControlStore(control_path, project)
    session = SQLiteBarrierSessionStore(control, lambda: "authority-rollback")
    session.create(identity)
    session.bind_child(
        1,
        BarrierChildIdentity.bind(identity, "rollback-forward", "new"),
    )


def _factory_binding(adapter: object, backend: str, operation_id: str) -> LiveUpgradeBinding:
    binding = object.__new__(LiveUpgradeBinding)
    object.__setattr__(
        binding,
        "runtime",
        SimpleNamespace(
            runtime_envelope={
                "backend": backend,
                "operation_id": operation_id,
                "state_revision": 2,
                "durable_barrier_id": "barrier-rollback",
                "fencing_token": "fence-rollback",
                "target": "rollback",
            }
        ),
    )
    object.__setattr__(binding, "adapter", adapter)
    return binding


class ConcreteRollbackEffectTests(unittest.TestCase):
    def test_git_restores_verified_backup_into_bound_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            repository = workspace / "repository"
            repository.mkdir()
            _git(repository, "init", "-b", "main")
            _git(repository, "config", "user.name", "Test Runner")
            _git(repository, "config", "user.email", "test@example.invalid")
            (repository / "state").write_text("old\n", encoding="utf-8")
            _git(repository, "add", "state")
            _git(repository, "commit", "-m", "initial")
            initial_head = _git(repository, "rev-parse", "HEAD")
            adapter = GitAuthorityAdapter(repository)
            backup = workspace / "git-backup"
            adapter.create_backup_bound(backup, quiesced=True)
            (repository / "state").write_text("new\n", encoding="utf-8")
            _git(repository, "add", "state")
            _git(repository, "commit", "-m", "forward")
            forward_head = _git(repository, "rev-parse", "HEAD")
            result = adapter.restore_authority_bound(
                backup, expected_branch="main", expected_head=forward_head
            )

            self.assertEqual(initial_head, _git(repository, "rev-parse", "HEAD"))
            self.assertEqual("old\n", (repository / "state").read_text(encoding="utf-8"))
            self.assertTrue(result["verified"])
            self.assertTrue(result["mutates_authority"])

    def test_sqlite_restores_verified_backup_into_bound_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = root / "authority.sqlite"
            with closing(sqlite3.connect(authority)) as connection, connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.executemany(
                    "INSERT INTO metadata VALUES (?, ?)",
                    [
                        ("schema_version", "1"),
                        ("backend", "sqlite"),
                        ("project_id", BINDING["project_id"]),
                        ("state_repository", BINDING["state_repository"]),
                        ("product_repository", BINDING["product_repository"]),
                        ("state", "active"),
                    ],
                )
                connection.execute(
                    "CREATE TABLE state (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute("INSERT INTO state VALUES (1, 'old')")
            authority.chmod(0o600)
            adapter = SQLiteAuthorityAdapter(authority)
            backup = root / "sqlite-backup.sqlite3"
            manifest = adapter.backup_bound(backup, BINDING)
            with closing(sqlite3.connect(authority)) as connection, connection:
                connection.execute("UPDATE state SET value='new' WHERE id=1")

            adapter.restore_bound(backup, authority, manifest, BINDING)

            with closing(sqlite3.connect(authority)) as connection:
                self.assertEqual(("old",), connection.execute("SELECT value FROM state").fetchone())

    def test_git_rollback_rejects_replaced_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            _git(repository, "init", "-b", "main")
            _git(repository, "config", "user.name", "Test Runner")
            _git(repository, "config", "user.email", "test@example.invalid")
            (repository / "state").write_text("old\n", encoding="utf-8")
            _git(repository, "add", "state")
            _git(repository, "commit", "-m", "initial")
            adapter = GitAuthorityAdapter(repository)
            backup = root / "backup"
            adapter.create_backup_bound(backup, quiesced=True)
            head = _git(repository, "rev-parse", "HEAD")
            repository.rename(root / "replaced")
            with self.assertRaises(GitAuthorityError):
                adapter.restore_authority_bound(backup, expected_branch="main", expected_head=head)

    def test_sqlite_rollback_rejects_replaced_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = root / "authority.sqlite"
            with closing(sqlite3.connect(authority)) as connection, connection:
                connection.execute(
                    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.executemany(
                    "INSERT INTO metadata VALUES (?, ?)",
                    [
                        ("schema_version", "1"),
                        ("backend", "sqlite"),
                        ("project_id", BINDING["project_id"]),
                        ("state_repository", BINDING["state_repository"]),
                        ("product_repository", BINDING["product_repository"]),
                        ("state", "active"),
                    ],
                )
                connection.execute("CREATE TABLE state (value TEXT)")
                connection.execute("INSERT INTO state VALUES ('old')")
            authority.chmod(0o600)
            adapter = SQLiteAuthorityAdapter(authority)
            backup = root / "backup.sqlite3"
            manifest = adapter.backup_bound(backup, BINDING)
            replaced = root / "replaced.sqlite"
            authority.rename(replaced)
            shutil.copy2(replaced, authority)
            authority.chmod(0o600)
            with self.assertRaises(SQLiteAuthorityError):
                adapter.restore_bound(backup, authority, manifest, BINDING)

    def test_git_concrete_rollback_is_durably_journaled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            repository = workspace / "repository"
            repository.mkdir()
            _git(repository, "init", "-b", "main")
            _git(repository, "config", "user.name", "Test Runner")
            _git(repository, "config", "user.email", "test@example.invalid")
            (repository / "state").write_text("old\n", encoding="utf-8")
            _git(repository, "add", "state")
            _git(repository, "commit", "-m", "initial")
            backup = workspace / "git-backup"
            adapter = GitAuthorityAdapter(repository)
            adapter.create_backup_bound(backup, quiesced=True)
            (repository / "state").write_text("new\n", encoding="utf-8")
            _git(repository, "add", "state")
            _git(repository, "commit", "-m", "forward")
            forward_head = _git(repository, "rev-parse", "HEAD")
            manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
            admission = _rollback_admission(
                artifact_identity=cast(str, manifest["commit"]),
                manifest_identity=hashlib.sha256(
                    json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            )

            project = str(uuid.uuid4())
            record: dict[str, object] = {
                "schema_version": 1,
                "project_id": project,
                "attempt_id": "rollback-attempt",
                "state_revision": 1,
                "authority_revision_at_acquire": "authority-rollback",
                "durable_barrier_id": "barrier-rollback",
                "fencing_token": "fence-rollback",
                "fencing_owner": "owner-rollback",
                "identity_digest": "0" * 64,
            }
            record["identity_digest"] = canonical_barrier_session_digest(record)
            identity = BarrierSessionIdentity.from_record(record)
            control = SQLiteRollbackControlStore(workspace / "control.sqlite", project)
            session = SQLiteBarrierSessionStore(control, lambda: "authority-rollback")
            session.create(identity)
            session.bind_child(
                1,
                BarrierChildIdentity.bind(identity, "real-git-rollback:forward", "new"),
            )
            live = object.__new__(LiveUpgradeBinding)
            object.__setattr__(
                live,
                "runtime",
                SimpleNamespace(
                    runtime_envelope={
                        "backend": "git",
                        "operation_id": "real-git-rollback",
                        "state_revision": 2,
                        "durable_barrier_id": "barrier-rollback",
                        "fencing_token": "fence-rollback",
                        "target": "rollback",
                    }
                ),
            )
            object.__setattr__(live, "adapter", adapter)

            with patch.object(LiveUpgradeBinding, "is_admitted", return_value=True):
                with self.assertRaises(ProductionRollbackEffectError):
                    bind_concrete_durable_rollback_capability(
                        live,
                        replace(admission, artifact_identity="wrong-artifact"),
                        session,
                        backup,
                        session_revision=2,
                        expected_branch="main",
                        expected_head=forward_head,
                    )
                capability = bind_concrete_durable_rollback_capability(
                    live,
                    admission,
                    session,
                    backup,
                    session_revision=2,
                    expected_branch="main",
                    expected_head=forward_head,
                )
            receipt = capability.execute(None)

            self.assertTrue(receipt.mutates_authority)
            self.assertEqual("old\n", (repository / "state").read_text(encoding="utf-8"))
            with closing(sqlite3.connect(control.path)) as check:
                self.assertEqual(
                    [("real-git-rollback:rollback", "committed")],
                    check.execute(
                        "SELECT operation_id, outcome FROM authority_effect_intent "
                        "WHERE project_id=?",
                        (project,),
                    ).fetchall(),
                )

    def test_git_rollback_process_death_recovers_as_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            repository = workspace / "repository"
            repository.mkdir()
            _git(repository, "init", "-b", "main")
            _git(repository, "config", "user.name", "Test Runner")
            _git(repository, "config", "user.email", "test@example.invalid")
            (repository / "state").write_text("old\n", encoding="utf-8")
            _git(repository, "add", "state")
            _git(repository, "commit", "-m", "initial")
            backup = workspace / "git-backup"
            adapter = GitAuthorityAdapter(repository)
            adapter.create_backup_bound(backup, quiesced=True)
            (repository / "state").write_text("new\n", encoding="utf-8")
            _git(repository, "add", "state")
            _git(repository, "commit", "-m", "forward")
            forward_head = _git(repository, "rev-parse", "HEAD")
            project = str(uuid.uuid4())
            control_path = workspace / "control.sqlite"
            _seed_rollback_session(control_path, project)

            worker = multiprocessing.get_context("fork").Process(
                target=_kill_after_git_rollback,
                args=(str(control_path), project, str(repository), str(backup), forward_head),
            )
            worker.start()
            worker.join(timeout=10)
            self.assertEqual(-signal.SIGKILL, worker.exitcode)

            recovered = SQLiteBarrierSessionStore(
                SQLiteRollbackControlStore(control_path, project),
                lambda: "authority-rollback",
            )
            state = recovered.recover_unknown()
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual("ambiguous", state.status)
            self.assertEqual("old\n", (repository / "state").read_text(encoding="utf-8"))
            with closing(sqlite3.connect(control_path)) as check:
                self.assertEqual(
                    [("ambiguous",)],
                    check.execute(
                        "SELECT outcome FROM authority_effect_intent WHERE project_id=?",
                        (project,),
                    ).fetchall(),
                )

    def test_sqlite_rollback_process_death_recovers_as_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = root / "authority.sqlite"
            with closing(sqlite3.connect(authority)) as connection, connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.executemany(
                    "INSERT INTO metadata VALUES (?, ?)",
                    [
                        ("schema_version", "1"),
                        ("backend", "sqlite"),
                        ("project_id", BINDING["project_id"]),
                        ("state_repository", BINDING["state_repository"]),
                        ("product_repository", BINDING["product_repository"]),
                        ("state", "active"),
                    ],
                )
                connection.execute(
                    "CREATE TABLE state (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute("INSERT INTO state VALUES (1, 'old')")
            authority.chmod(0o600)
            adapter = SQLiteAuthorityAdapter(authority)
            backup = root / "sqlite-backup.sqlite3"
            manifest = adapter.backup_bound(backup, BINDING)
            with closing(sqlite3.connect(authority)) as connection, connection:
                connection.execute("UPDATE state SET value='new' WHERE id=1")
            project = str(uuid.uuid4())
            control_path = root / "control.sqlite"
            _seed_rollback_session(control_path, project)
            journal = SQLiteBarrierSessionStore(
                SQLiteRollbackControlStore(control_path, project),
                lambda: "authority-rollback",
            )
            sqlite_admission = CommitAdmissionBundle(
                backend="sqlite",
                target="rollback",
                operation_id="real-sqlite-rollback:rollback",
                fencing_token="fence-rollback",  # noqa: S106
                state_revision=2,
                barrier_id="barrier-rollback",
                artifact_identity=cast(str, manifest["database_sha256"]),
                manifest_identity=hashlib.sha256(
                    json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                selector_identity="selector-rollback",
                runtime_identity="runtime-rollback",
            )
            mismatch_backup = root / "mismatch.sqlite3"
            shutil.copy2(backup, mismatch_backup)
            with (
                patch.object(LiveUpgradeBinding, "is_admitted", return_value=True),
                self.assertRaises(ProductionRollbackEffectError),
            ):
                bind_concrete_durable_rollback_capability(
                    _factory_binding(adapter, "sqlite", "real-sqlite-rollback"),
                    replace(sqlite_admission, artifact_identity="wrong-artifact"),
                    journal,
                    mismatch_backup,
                    session_revision=2,
                    sqlite_manifest=manifest,
                    sqlite_binding=BINDING,
                )

            worker = multiprocessing.get_context("fork").Process(
                target=_kill_after_sqlite_rollback,
                args=(str(control_path), project, str(authority), str(backup), manifest),
            )
            worker.start()
            worker.join(timeout=10)
            self.assertEqual(-signal.SIGKILL, worker.exitcode)

            recovered = SQLiteBarrierSessionStore(
                SQLiteRollbackControlStore(control_path, project),
                lambda: "authority-rollback",
            )
            state = recovered.recover_unknown()
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual("ambiguous", state.status)
            with closing(sqlite3.connect(authority)) as check:
                self.assertEqual(("old",), check.execute("SELECT value FROM state").fetchone())
            with closing(sqlite3.connect(control_path)) as check:
                self.assertEqual(
                    [("ambiguous",)],
                    check.execute(
                        "SELECT outcome FROM authority_effect_intent WHERE project_id=?",
                        (project,),
                    ).fetchall(),
                )


if __name__ == "__main__":
    unittest.main()
