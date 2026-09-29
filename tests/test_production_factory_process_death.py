# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
# ruff: noqa: S603, S607

"""Fresh-process death qualification for concrete production effect factories."""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, NoReturn

from tools.authority_neutral_commit import CommitAdmissionBundle
from tools.git_authority_adapter import GitAuthorityAdapter
from tools.rollback_control_store import SQLiteBarrierSessionStore, SQLiteRollbackControlStore
from tools.sqlite_authority_adapter import SQLiteAuthorityAdapter
from tools.upgrade_identity import (
    BarrierChildIdentity,
    BarrierSessionIdentity,
    canonical_barrier_session_digest,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _identity(project: str, backend: str) -> BarrierSessionIdentity:
    record: dict[str, object] = {
        "schema_version": 1,
        "project_id": project,
        "attempt_id": f"factory-death-{backend}",
        "state_revision": 1,
        "authority_revision_at_acquire": f"authority-{backend}",
        "durable_barrier_id": f"barrier-{backend}",
        "fencing_token": f"fence-{backend}",
        "fencing_owner": f"owner-{backend}",
        "identity_digest": "0" * 64,
    }
    record["identity_digest"] = canonical_barrier_session_digest(record)
    return BarrierSessionIdentity.from_record(record)


def _admission(backend: str) -> CommitAdmissionBundle:
    return CommitAdmissionBundle(
        backend=backend,
        target="new",
        operation_id=f"factory-death-{backend}:commit",
        fencing_token=f"fence-{backend}",
        state_revision=3,
        barrier_id=f"barrier-{backend}",
        artifact_identity=f"artifact-{backend}",
        manifest_identity=f"manifest-{backend}",
        selector_identity=f"selector-{backend}",
        runtime_identity=f"runtime-{backend}",
    )


def _kill_before_outcome_git(root_text: str, project: str, ready: Any) -> NoReturn:
    root = Path(root_text)
    control = SQLiteRollbackControlStore(
        root / "control.sqlite", project, root / "authority.sqlite"
    )
    session = SQLiteBarrierSessionStore(control, lambda: "authority-git")
    admission = _admission("git")
    repository = root / "repository"
    expected_head = _git(repository, "rev-parse", "HEAD")
    capability = GitAuthorityAdapter(repository).bind_durable_commit_capability(
        admission,
        session,
        session_revision=3,
        admission_reread=lambda: admission.__dict__,
        expected_branch="main",
        expected_head=expected_head,
    )

    def kill(*_args: object, **_kwargs: object) -> NoReturn:
        ready.set()
        os._exit(17)

    session_any: Any = session
    session_any.finish_authority_effect = kill
    capability.execute(
        {
            "message": "factory death select runtime",
            "path": ".runtime/runtime-selector.json",
            "content": '{"schema_version":1,"active_release":"v0.2.0"}\n',
        }
    )
    raise AssertionError("worker survived injected death")


def _kill_before_outcome_sqlite(root_text: str, project: str, ready: Any) -> NoReturn:
    root = Path(root_text)
    database = root / "authority.sqlite"
    control = SQLiteRollbackControlStore(root / "control.sqlite", project, database)
    session = SQLiteBarrierSessionStore(control, lambda: "authority-sqlite")
    admission = _admission("sqlite")
    capability = SQLiteAuthorityAdapter(database).bind_durable_commit_capability(
        admission,
        session,
        session_revision=3,
        admission_reread=lambda: admission.__dict__,
    )

    def kill(*_args: object, **_kwargs: object) -> NoReturn:
        ready.set()
        os._exit(17)

    session_any: Any = session
    session_any.finish_authority_effect = kill

    def effect(connection: sqlite3.Connection) -> None:
        connection.execute("UPDATE state SET value='v0.2.0' WHERE id=1")

    capability.execute(effect)
    raise AssertionError("worker survived injected death")


class ProductionFactoryProcessDeathTests(unittest.TestCase):
    def _session(self, root: Path, project: str, backend: str) -> None:
        authority = root / "authority.sqlite"
        control = SQLiteRollbackControlStore(root / "control.sqlite", project, authority)
        session = SQLiteBarrierSessionStore(control, lambda: f"authority-{backend}")
        identity = _identity(project, backend)
        session.create(identity)
        operation = f"factory-death-{backend}:commit"
        session.bind_child(1, BarrierChildIdentity.bind(identity, operation, "new"))
        session.bind_child(
            2,
            BarrierChildIdentity.bind(identity, f"factory-death-{backend}:rollback", "rollback"),
        )

    def test_git_factory_effect_death_is_ambiguous_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir(mode=0o700)
            _git(repository, "init", "-b", "main")
            (repository / ".git").chmod(0o700)
            _git(repository, "config", "user.name", "Factory Runner")
            _git(repository, "config", "user.email", "factory@example.invalid")
            runtime = repository / ".runtime"
            runtime.mkdir(mode=0o700)
            (runtime / "runtime-selector.json").write_text(
                '{"schema_version":1,"active_release":"v0.1.0"}\n', encoding="utf-8"
            )
            _git(repository, "add", ".runtime/runtime-selector.json")
            _git(repository, "commit", "-m", "initial runtime")
            authority = root / "authority.sqlite"
            with closing(sqlite3.connect(authority)) as connection, connection:
                connection.execute("CREATE TABLE marker (value TEXT)")
            authority.chmod(0o600)
            project = str(uuid.uuid4())
            self._session(root, project, "git")
            context = multiprocessing.get_context("fork")
            ready = context.Event()
            worker = context.Process(
                target=_kill_before_outcome_git, args=(str(root), project, ready)
            )
            worker.start()
            self.assertTrue(ready.wait(5))
            worker.join(5)
            self.assertEqual(17, worker.exitcode)
            self.assertIn(
                '"active_release":"v0.2.0"', (runtime / "runtime-selector.json").read_text()
            )
            control = SQLiteRollbackControlStore(root / "control.sqlite", project, authority)
            reopened = SQLiteBarrierSessionStore(control, lambda: "authority-git")
            held = reopened.snapshot()
            self.assertIsNotNone(held)
            assert held is not None
            ambiguous = reopened.recover_unknown()
            self.assertEqual(
                ("ambiguous", held.revision + 1), (ambiguous.status, ambiguous.revision)
            )
            with self.assertRaisesRegex(Exception, "recovery|ambiguous|fence|held"):
                reopened.prepare_authority_effect(held.revision, "factory-death-git:commit", "git")

    def test_sqlite_factory_effect_death_is_ambiguous_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authority = root / "authority.sqlite"
            with closing(sqlite3.connect(authority)) as connection, connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE state (id INTEGER PRIMARY KEY, value TEXT)")
                connection.execute("INSERT INTO state VALUES (1, 'v0.1.0')")
            authority.chmod(0o600)
            project = str(uuid.uuid4())
            self._session(root, project, "sqlite")
            context = multiprocessing.get_context("fork")
            ready = context.Event()
            worker = context.Process(
                target=_kill_before_outcome_sqlite, args=(str(root), project, ready)
            )
            worker.start()
            self.assertTrue(ready.wait(5))
            worker.join(5)
            self.assertEqual(17, worker.exitcode)
            with closing(sqlite3.connect(authority)) as connection:
                self.assertEqual(
                    ("v0.2.0",), connection.execute("SELECT value FROM state").fetchone()
                )
            control = SQLiteRollbackControlStore(root / "control.sqlite", project, authority)
            reopened = SQLiteBarrierSessionStore(control, lambda: "authority-sqlite")
            held = reopened.snapshot()
            self.assertIsNotNone(held)
            assert held is not None
            ambiguous = reopened.recover_unknown()
            self.assertEqual(
                ("ambiguous", held.revision + 1), (ambiguous.status, ambiguous.revision)
            )


if __name__ == "__main__":
    unittest.main()
