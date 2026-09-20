# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Independent-process tests for normal SQLite writers at the upgrade barrier."""

from __future__ import annotations

import multiprocessing
import signal
import sqlite3
import tempfile
import unittest
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import tools.handoffctl as handoffctl
from tools.admission_lease import AdmissionLease, validate_recheck
from tools.handoffctl import locked
from tools.lock_domain_scope import LockDomainScope
from tools.mutation_fence import MutationFence, provision, provision_control_binding
from tools.rollback_control_store import (
    BarrierSessionState,
    SQLiteBarrierSessionStore,
    SQLiteRollbackControlStore,
)
from tools.sqlite_storage import SQLiteBackend, bind_released_sqlite_backend, create_database
from tools.upgrade_identity import (
    BarrierChildIdentity,
    BarrierSessionIdentity,
    canonical_barrier_session_digest,
)

PROJECT = "11111111-1111-4111-8111-111111111111"
BINDING = {
    "project_id": PROJECT,
    "state_repository": "owner/state",
    "product_repository": "owner/product",
}


def _identity(
    *,
    attempt: str = "attempt-1",
    state_revision: int = 1,
    barrier: str = "barrier-1",
    fence: str = "fence-1",
    owner: str = "owner-1",
) -> BarrierSessionIdentity:
    record: dict[str, object] = {
        "schema_version": 1,
        "project_id": PROJECT,
        "attempt_id": attempt,
        "state_revision": state_revision,
        "authority_revision_at_acquire": "authority-1",
        "durable_barrier_id": barrier,
        "fencing_token": fence,
        "fencing_owner": owner,
        "identity_digest": "0" * 64,
    }
    record["identity_digest"] = canonical_barrier_session_digest(record)
    return BarrierSessionIdentity.from_record(record)


def _control(root: Path) -> SQLiteRollbackControlStore:
    return SQLiteRollbackControlStore(root / "control.sqlite", PROJECT, root / "authority.sqlite")


def _fence(root: Path, control: SQLiteRollbackControlStore) -> MutationFence:
    return MutationFence(
        root / "authority.sqlite",
        root / "authority-marker.json",
        root / "authority-lifecycle.json",
        root / "authority.lock",
        control.control_store_path,
        root / "control-binding.json",
        control.control_lock_path,
    )


def _backend(root: Path, fence: MutationFence) -> SQLiteBackend:
    return SQLiteBackend(
        root / "authority.sqlite",
        BINDING,
        root / "tasks",
        mutation_scope=lambda: fence.mutation_scope(locked),
    )


def _normal_writer(root_text: str, result: Any, start: Any | None = None) -> None:
    """Run one public mutation route from a freshly constructed process."""
    root = Path(root_text)
    control = _control(root)
    backend = _backend(root, _fence(root, control))
    if start is not None:
        start.wait()

    def update_summary(
        meta: dict[str, Any], _tasks: list[tuple[Path, dict[str, Any], str]]
    ) -> tuple[str, str]:
        meta["summary"] = "mutated after verified release"
        return "released writer", "# Authority fixture\n\nWriter committed.\n"

    try:
        backend.mutate(
            "AR-0001",
            1,
            "update",
            "2026-09-16T15:10:00+00:00",
            update_summary,
        )
    except Exception as error:
        result.put(("rejected", type(error).__name__, str(error)))
    else:
        result.put(("committed",))


def _held_scope_owner(root_text: str, ready: Any) -> None:
    """Hold the real common/control/authority scope until externally killed."""
    root = Path(root_text)
    control = _control(root)
    session = SQLiteBarrierSessionStore(control, lambda: "authority-1")
    state = session.snapshot()
    assert state is not None
    identity = state.identity
    lease = AdmissionLease(
        PROJECT,
        identity.authority_revision_at_acquire,
        identity.fencing_token,
        identity.fencing_owner,
        identity.durable_barrier_id,
        identity.state_revision,
    )
    recheck = validate_recheck(
        lease,
        project_id=PROJECT,
        authority_revision=identity.authority_revision_at_acquire,
        fencing_token=identity.fencing_token,
        fencing_owner=identity.fencing_owner,
        durable_barrier_id=identity.durable_barrier_id,
        revision=identity.state_revision,
    )
    scope = LockDomainScope.bind(session, _fence(root, control), lease, recheck, locked)
    with scope.hold():
        ready.set()
        multiprocessing.Event().wait()


def _session_commit_waiting_for_sigkill(root_text: str, ready: Any) -> None:
    """Pause after durable CAS commit but before intent outcome publication."""
    root = Path(root_text)
    session = SQLiteBarrierSessionStore(_control(root), lambda: "authority-1")

    def wait_after_commit(*_args: object, **_kwargs: object) -> None:
        ready.set()
        multiprocessing.Event().wait()

    session_any: Any = session
    session_any._mark_intent_locked = wait_after_commit
    session.create(_identity())


def _session_recovery_waiting_for_sigkill(root_text: str, ready: Any) -> None:
    """Pause after durable ambiguity but before intent outcome publication."""
    root = Path(root_text)
    session = SQLiteBarrierSessionStore(_control(root), lambda: "authority-1")

    def wait_after_ambiguity(*_args: object, **_kwargs: object) -> None:
        ready.set()
        multiprocessing.Event().wait()

    session_any: Any = session
    session_any._mark_intent_locked = wait_after_ambiguity
    session.recover_unknown()


def _reconcile_waiting_for_sigkill(root_text: str, ready: Any) -> None:
    """Lose the reply after reconciliation commits before outcome publication."""
    root = Path(root_text)
    session = SQLiteBarrierSessionStore(_control(root), lambda: "authority-1")
    ambiguous = session.snapshot()
    assert ambiguous is not None
    replacement = BarrierSessionState(
        _identity(
            attempt="attempt-reconciled",
            state_revision=2,
            barrier="barrier-reconciled",
            fence="fence-reconciled",
            owner="owner-reconciled",
        ),
        "held",
        1,
    )

    def wait_before_outcome(*_args: object, **_kwargs: object) -> None:
        ready.set()
        multiprocessing.Event().wait()

    session_any: Any = session
    session_any._mark_intent_locked = wait_before_outcome
    session.reconcile_ambiguous(ambiguous.revision, replacement)


def _route_effect_waiting_for_sigkill(root_text: str, ready: Any) -> None:
    """Pause after a public route's SQL effects and before transaction commit."""
    root = Path(root_text)
    backend = _backend(root, _fence(root, _control(root)))
    checks = 0

    def wait_before_commit() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            ready.set()
            multiprocessing.Event().wait()

    backend_any: Any = backend
    backend_any._assert_mutation_binding = wait_before_commit

    def update_summary(
        meta: dict[str, Any], _tasks: list[tuple[Path, dict[str, Any], str]]
    ) -> tuple[str, str]:
        meta["summary"] = "must roll back after SIGKILL"
        return "killed before commit", "# Must not persist\n"

    backend.mutate("AR-0001", 1, "update", "2026-09-16T15:11:00+00:00", update_summary)


def _route_effect_waiting_for_sidecar_fault(
    root_text: str, ready: Any, resume: Any, result: Any
) -> None:
    """Pause a real mutation after WAL effects so its sidecars can be replaced."""
    root = Path(root_text)
    backend = _backend(root, _fence(root, _control(root)))
    original_assert = backend._assert_mutation_binding
    checks = 0

    def wait_before_commit() -> None:
        nonlocal checks
        checks += 1
        original_assert()
        if checks == 2:
            ready.set()
            if not resume.wait(5):
                raise RuntimeError("authority sidecar fault injection timed out")

    backend_any: Any = backend
    backend_any._assert_mutation_binding = wait_before_commit

    def update_summary(
        meta: dict[str, Any], _tasks: list[tuple[Path, dict[str, Any], str]]
    ) -> tuple[str, str]:
        meta["summary"] = "must roll back after WAL replacement"
        return "replace active WAL", "# Must not persist\n"

    try:
        backend.mutate(
            "AR-0001",
            1,
            "update",
            "2026-09-16T15:12:00+00:00",
            update_summary,
        )
    except Exception as error:
        result.put(("rejected", type(error).__name__, str(error)))
    else:
        result.put(("committed",))


class SQLiteMutationBarrierProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.root.chmod(0o700)
        self.authority = self.root / "authority.sqlite"
        self.tasks = self.root / "tasks"
        self.tasks.mkdir()
        task = {
            "schema_version": 1,
            "id": "AR-0001",
            "title": "Authority fixture task",
            "status": "open",
            "priority": "P1",
            "summary": "Ready.",
            "next_action": "Exercise the independent writer.",
            "task_revision": 1,
            "updated_at": "2026-09-16T00:00:00+00:00",
            "owner": "",
            "claim_expires": "",
            "worktree_key": "worker-1",
            "branch": "",
            "checkpoint_commit": "",
            "plan": "",
            "depends_on": [],
        }
        create_database(
            self.authority,
            BINDING,
            [(Path("AR-0001-authority-fixture.md"), task, "# Authority fixture\n")],
            imported_at="2026-09-16T00:00:00+00:00",
            source_backend="git",
            source_checkpoint="a" * 40,
        )
        self.control = SQLiteRollbackControlStore(
            self.root / "control.sqlite", PROJECT, self.authority
        )
        self.session = SQLiteBarrierSessionStore(self.control, lambda: "authority-1")
        self.assertIsNone(self.session.snapshot())
        provision(
            self.authority,
            self.root / "authority-marker.json",
            self.root / "authority-lifecycle.json",
            self.root / "authority.lock",
            PROJECT,
        )
        provision_control_binding(
            self.control.control_store_path,
            self.root / "control-binding.json",
            self.control.control_lock_path,
            PROJECT,
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _create_held(self) -> BarrierSessionState:
        return self.session.create(_identity())

    def _kill(self, process: BaseProcess) -> None:
        process.kill()
        process.join(5)
        self.assertEqual(-signal.SIGKILL, process.exitcode)

    def _run_writer(self) -> tuple[str, ...]:
        context = multiprocessing.get_context("fork")
        result = context.Queue()
        worker = context.Process(target=_normal_writer, args=(self.directory.name, result))
        worker.start()
        worker.join(5)
        self.assertEqual(0, worker.exitcode)
        return cast(tuple[str, ...], result.get(timeout=1))

    def _authority_revision(self) -> tuple[int, str]:
        with sqlite3.connect(self.authority) as connection:
            row = connection.execute(
                "SELECT revision, meta_json FROM tasks WHERE id='AR-0001'"
            ).fetchone()
        assert row is not None
        return int(row[0]), str(row[1])

    def _release(self, held: BarrierSessionState) -> BarrierSessionState:
        child = BarrierChildIdentity("forward-1", "new", held.identity.identity_digest)
        bound = self.session.bind_child(held.revision, child)
        releasing = self.session.begin_reopen(bound.revision, "new")
        return self.session.complete_reopen(releasing.revision, True)

    def test_released_writer_factory_cannot_bypass_held_barrier(self) -> None:
        self._create_held()
        fence = _fence(self.root, self.control)
        backend = bind_released_sqlite_backend(
            self.authority,
            BINDING,
            self.tasks,
            fence,
            locked,
        )

        def update_summary(
            meta: dict[str, Any], _tasks: list[tuple[Path, dict[str, Any], str]]
        ) -> tuple[str, str]:
            meta["summary"] = "must not commit while held"
            return "rejected", "# Must not persist\n"

        with self.assertRaisesRegex(
            RuntimeError, "authority mutation rejected while barrier is held"
        ):
            backend.mutate(
                "AR-0001",
                1,
                "update",
                "2026-09-21T00:00:00+00:00",
                update_summary,
            )
        self.assertEqual(1, self._authority_revision()[0])

    def test_handoffctl_factory_uses_bound_route_when_provisioned(self) -> None:
        self._create_held()
        with (
            patch.object(handoffctl, "DATABASE", self.authority),
            patch.object(handoffctl, "CONTROL_DATABASE", self.control.control_store_path),
            patch.object(handoffctl, "AUTHORITY_MARKER", self.root / "authority-marker.json"),
            patch.object(handoffctl, "AUTHORITY_LIFECYCLE", self.root / "authority-lifecycle.json"),
            patch.object(handoffctl, "AUTHORITY_LOCK", self.root / "authority.lock"),
            patch.object(handoffctl, "CONTROL_BINDING", self.root / "control-binding.json"),
            patch.object(handoffctl, "CONTROL_LOCK", self.control.control_lock_path),
            patch.object(handoffctl, "project_binding", return_value=BINDING),
            patch.object(handoffctl, "TASKS", self.tasks),
        ):
            backend = handoffctl.mutating_sqlite_backend()
        self.assertEqual("sqlite", backend.name)
        self.assertIsNotNone(backend.backend_binding)

    def test_bound_route_commits_after_independently_verified_release(self) -> None:
        held = self._create_held()
        self._release(held)
        backend = bind_released_sqlite_backend(
            self.authority,
            BINDING,
            self.tasks,
            _fence(self.root, self.control),
            locked,
        )

        def update_summary(
            meta: dict[str, Any], _tasks: list[tuple[Path, dict[str, Any], str]]
        ) -> tuple[str, str]:
            meta["summary"] = "committed after verified release"
            return "released writer", "# Authority fixture\n\nWriter committed.\n"

        backend.mutate(
            "AR-0001",
            1,
            "update",
            "2026-09-21T00:01:00+00:00",
            update_summary,
        )
        self.assertEqual(2, self._authority_revision()[0])

    def test_independent_writer_rejects_until_verified_release(self) -> None:
        self.assertEqual(
            (
                "rejected",
                "MutationFenceError",
                "durable control barrier is missing or ambiguous",
            ),
            self._run_writer(),
        )
        held = self._create_held()
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        holder = context.Process(target=_held_scope_owner, args=(self.directory.name, ready))
        holder.start()
        self.assertTrue(ready.wait(5))
        self._kill(holder)
        self.assertEqual(held, self.session.snapshot())

        self.assertEqual(
            ("rejected", "MutationFenceError", "authority mutation rejected while barrier is held"),
            self._run_writer(),
        )
        self.assertEqual(1, self._authority_revision()[0])

        child = BarrierChildIdentity("forward-1", "new", held.identity.identity_digest)
        bound = self.session.bind_child(held.revision, child)
        releasing = self.session.begin_reopen(bound.revision, "new")
        self.assertEqual(
            (
                "rejected",
                "MutationFenceError",
                "authority mutation rejected while barrier is releasing",
            ),
            self._run_writer(),
        )
        self.assertEqual(1, self._authority_revision()[0])

        released = self.session.complete_reopen(releasing.revision, True)
        self.assertEqual("released", released.status)
        self.assertEqual(("committed",), self._run_writer())
        revision, meta_json = self._authority_revision()
        self.assertEqual(2, revision)
        self.assertIn("mutated after verified release", meta_json)

    def test_sigkill_after_route_effects_rolls_back_and_releases_locks(self) -> None:
        released = self._release(self._create_held())
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        crashed = context.Process(
            target=_route_effect_waiting_for_sigkill,
            args=(self.directory.name, ready),
        )
        crashed.start()
        self.assertTrue(ready.wait(5))
        self._kill(crashed)

        revision, meta_json = self._authority_revision()
        self.assertEqual(1, revision)
        self.assertIn('"summary": "Ready."', meta_json)
        self.assertNotIn("must roll back after SIGKILL", meta_json)
        with sqlite3.connect(self.authority) as connection:
            events = connection.execute(
                "SELECT revision, kind FROM events WHERE task_id='AR-0001' ORDER BY revision"
            ).fetchall()
        self.assertEqual([(1, "import")], events)
        self.assertEqual(released, self.session.snapshot())
        self.assertEqual(("committed",), self._run_writer())
        self.assertEqual(2, self._authority_revision()[0])

    def test_active_authority_wal_replacement_rejects_without_publication(self) -> None:
        released = self._release(self._create_held())
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        resume = context.Event()
        result = context.Queue()
        writer = context.Process(
            target=_route_effect_waiting_for_sidecar_fault,
            args=(self.directory.name, ready, resume, result),
        )
        writer.start()
        self.assertTrue(ready.wait(5))

        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.authority}{suffix}")
            self.assertTrue(sidecar.is_file())
            sidecar.unlink()
            sidecar.write_bytes(b"hostile authority sidecar replacement")
            sidecar.chmod(0o600)
        resume.set()
        writer.join(5)
        self.assertEqual(0, writer.exitcode)
        outcome = cast(tuple[str, ...], result.get(timeout=1))
        self.assertEqual("rejected", outcome[0])
        self.assertNotEqual(("committed",), outcome)

        for suffix in ("-wal", "-shm"):
            Path(f"{self.authority}{suffix}").unlink(missing_ok=True)
        revision, meta_json = self._authority_revision()
        self.assertEqual(1, revision)
        self.assertIn('"summary": "Ready."', meta_json)
        self.assertNotIn("must roll back after WAL replacement", meta_json)
        with sqlite3.connect(self.authority) as connection:
            self.assertEqual(
                [(1, "import")],
                connection.execute(
                    "SELECT revision, kind FROM events WHERE task_id='AR-0001' ORDER BY revision"
                ).fetchall(),
            )
            self.assertEqual(("ok",), connection.execute("PRAGMA integrity_check").fetchone())
        self.assertEqual(released, self.session.snapshot())
        self.assertEqual(("committed",), self._run_writer())

    def test_forged_released_row_rejects_before_authority_mutation(self) -> None:
        held = self._create_held()
        with sqlite3.connect(self.control.control_store_path) as connection:
            connection.execute(
                "UPDATE barrier_session SET status='released',revision=?,identity_digest=? "
                "WHERE project_id=?",
                (held.revision + 1, "0" * 64, PROJECT),
            )
            connection.commit()

        self.assertEqual(
            (
                "rejected",
                "MutationFenceError",
                "durable control barrier state is invalid",
            ),
            self._run_writer(),
        )
        revision, meta_json = self._authority_revision()
        self.assertEqual(1, revision)
        self.assertIn('"summary": "Ready."', meta_json)
        self.assertNotIn("mutated after verified release", meta_json)
        with sqlite3.connect(self.authority) as connection:
            events = connection.execute(
                "SELECT revision, kind FROM events WHERE task_id='AR-0001' ORDER BY revision"
            ).fetchall()
            results = connection.execute("SELECT COUNT(*) FROM command_results").fetchone()
        self.assertEqual([(1, "import")], events)
        self.assertEqual((0,), results)

    def test_sigkill_session_commit_recovers_ambiguous_then_verified_release(self) -> None:
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        crashed = context.Process(
            target=_session_commit_waiting_for_sigkill,
            args=(self.directory.name, ready),
        )
        crashed.start()
        self.assertTrue(ready.wait(5))
        self._kill(crashed)

        committed = self.session.snapshot()
        assert committed is not None
        self.assertEqual(
            ("held", 1, _identity()),
            (
                committed.status,
                committed.revision,
                committed.identity,
            ),
        )
        ambiguous = self.session.recover_unknown()
        assert ambiguous is not None
        self.assertEqual(("ambiguous", 2), (ambiguous.status, ambiguous.revision))
        self.assertEqual(_identity(), ambiguous.identity)
        self.assertEqual(
            (
                "rejected",
                "MutationFenceError",
                "authority mutation rejected while barrier is ambiguous",
            ),
            self._run_writer(),
        )
        self.assertEqual(1, self._authority_revision()[0])

        replacement = BarrierSessionState(
            _identity(
                attempt="attempt-2",
                state_revision=2,
                barrier="barrier-2",
                fence="fence-2",
                owner="owner-2",
            ),
            "held",
            1,
        )
        reconciled = self.session.reconcile_ambiguous(ambiguous.revision, replacement)
        self.assertEqual(replacement, reconciled)
        self.assertEqual(
            ("rejected", "MutationFenceError", "authority mutation rejected while barrier is held"),
            self._run_writer(),
        )
        released = self._release(reconciled)
        self.assertEqual("released", released.status)
        self.assertEqual(("committed",), self._run_writer())
        self.assertEqual(2, self._authority_revision()[0])
        with sqlite3.connect(self.control.control_store_path) as connection:
            outcomes = connection.execute(
                "SELECT outcome FROM barrier_session_intent ORDER BY proposed_revision"
            ).fetchall()
        self.assertNotIn(("prepared",), outcomes)
        self.assertIn(("ambiguous",), outcomes)

    def test_sigkill_during_unknown_recovery_preserves_ambiguity_until_new_fence(self) -> None:
        context = multiprocessing.get_context("fork")
        commit_ready = context.Event()
        committed_without_outcome = context.Process(
            target=_session_commit_waiting_for_sigkill,
            args=(self.directory.name, commit_ready),
        )
        committed_without_outcome.start()
        self.assertTrue(commit_ready.wait(5))
        self._kill(committed_without_outcome)

        committed = self.session.snapshot()
        assert committed is not None
        self.assertEqual(("held", 1), (committed.status, committed.revision))

        recovery_ready = context.Event()
        crashed_recovery = context.Process(
            target=_session_recovery_waiting_for_sigkill,
            args=(self.directory.name, recovery_ready),
        )
        crashed_recovery.start()
        self.assertTrue(recovery_ready.wait(5))
        self._kill(crashed_recovery)

        reopened = SQLiteBarrierSessionStore(_control(self.root), lambda: "authority-1")
        ambiguous = reopened.snapshot()
        assert ambiguous is not None
        self.assertEqual(("ambiguous", 2), (ambiguous.status, ambiguous.revision))
        with sqlite3.connect(self.control.control_store_path) as connection:
            self.assertEqual(
                [("prepared",)],
                connection.execute(
                    "SELECT outcome FROM barrier_session_intent WHERE project_id=?",
                    (PROJECT,),
                ).fetchall(),
            )

        self.assertEqual(
            (
                "rejected",
                "MutationFenceError",
                "authority mutation rejected while barrier is ambiguous",
            ),
            self._run_writer(),
        )
        self.assertEqual(1, self._authority_revision()[0])

        self.assertEqual(ambiguous, reopened.recover_unknown())
        with sqlite3.connect(self.control.control_store_path) as connection:
            self.assertEqual(
                [("ambiguous",)],
                connection.execute(
                    "SELECT outcome FROM barrier_session_intent WHERE project_id=?",
                    (PROJECT,),
                ).fetchall(),
            )

        replacement = BarrierSessionState(
            _identity(
                attempt="attempt-recovered",
                state_revision=2,
                barrier="barrier-recovered",
                fence="fence-recovered",
                owner="owner-recovered",
            ),
            "held",
            1,
        )
        reconciled = reopened.reconcile_ambiguous(ambiguous.revision, replacement)
        self.assertEqual(replacement, reconciled)
        self.assertEqual(
            ("rejected", "MutationFenceError", "authority mutation rejected while barrier is held"),
            self._run_writer(),
        )
        self.session = reopened
        released = self._release(reconciled)
        self.assertEqual("released", released.status)
        self.assertEqual(("committed",), self._run_writer())
        self.assertEqual(2, self._authority_revision()[0])

    def test_sigkill_after_reconciliation_commit_recovers_new_fence_from_history(self) -> None:
        held = self._create_held()
        ambiguous = self.session.mark_ambiguous(held.revision, "process-death")
        self.assertEqual(("ambiguous", 2), (ambiguous.status, ambiguous.revision))

        context = multiprocessing.get_context("fork")
        ready = context.Event()
        crashed = context.Process(
            target=_reconcile_waiting_for_sigkill,
            args=(self.directory.name, ready),
        )
        crashed.start()
        self.assertTrue(ready.wait(5))
        self._kill(crashed)

        reopened = SQLiteBarrierSessionStore(_control(self.root), lambda: "authority-1")
        recovered = reopened.snapshot()
        assert recovered is not None
        self.assertEqual(("held", 1), (recovered.status, recovered.revision))
        self.assertEqual("attempt-reconciled", recovered.identity.attempt_id)
        self.assertEqual(2, recovered.identity.state_revision)
        self.assertNotEqual(ambiguous.identity.fencing_token, recovered.identity.fencing_token)
        with sqlite3.connect(self.control.control_store_path) as connection:
            history = connection.execute(
                "SELECT attempt_id,revision,record_json FROM barrier_session_history "
                "WHERE project_id=?",
                (PROJECT,),
            ).fetchall()
        self.assertEqual(1, len(history))
        self.assertEqual((ambiguous.identity.attempt_id, ambiguous.revision), history[0][:2])
        self.assertIn('"status":"ambiguous"', str(history[0][2]))

        with sqlite3.connect(self.control.control_store_path) as connection:
            intent_before = connection.execute(
                "SELECT expected_revision,proposed_revision,outcome "
                "FROM barrier_session_intent WHERE project_id=?",
                (PROJECT,),
            ).fetchall()
            connection.execute(
                "UPDATE barrier_session_intent SET expected_revision=999 "
                "WHERE project_id=? AND outcome='prepared'",
                (PROJECT,),
            )
            connection.commit()
        with self.assertRaisesRegex(Exception, "prepared session intent identity is invalid"):
            reopened.recover_unknown()
        self.assertEqual(recovered, reopened.snapshot())
        with sqlite3.connect(self.control.control_store_path) as connection:
            intent_after_rejected_recovery = connection.execute(
                "SELECT expected_revision,proposed_revision,outcome "
                "FROM barrier_session_intent WHERE project_id=?",
                (PROJECT,),
            ).fetchall()
        self.assertEqual(
            [(999 if row[2] == "prepared" else row[0], row[1], row[2]) for row in intent_before],
            intent_after_rejected_recovery,
        )

        # Restore the exact predecessor revision only to continue the valid
        # recovery path; the forged attempt above must not mutate any row.
        with sqlite3.connect(self.control.control_store_path) as connection:
            connection.execute(
                "UPDATE barrier_session_intent SET expected_revision=? "
                "WHERE project_id=? AND outcome='prepared'",
                (ambiguous.revision, PROJECT),
            )
            connection.commit()
        self.assertEqual(recovered, reopened.recover_unknown())

        self.assertEqual(
            ("rejected", "MutationFenceError", "authority mutation rejected while barrier is held"),
            self._run_writer(),
        )
        self.assertEqual(1, self._authority_revision()[0])
        with self.assertRaisesRegex(Exception, "only ambiguous sessions require reconciliation"):
            reopened.reconcile_ambiguous(ambiguous.revision, recovered)
        self.assertEqual(recovered, reopened.snapshot())

        self.session = reopened
        released = self._release(recovered)
        self.assertEqual("released", released.status)
        self.assertEqual(("committed",), self._run_writer())
        self.assertEqual(2, self._authority_revision()[0])

    def test_concurrent_released_writers_serialize_one_exact_revision(self) -> None:
        released = self._release(self._create_held())
        context = multiprocessing.get_context("fork")
        start = context.Event()
        result = context.Queue()
        workers = [
            context.Process(
                target=_normal_writer,
                args=(self.directory.name, result, start),
            )
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(5)
            self.assertEqual(0, worker.exitcode)

        outcomes = sorted([result.get(timeout=1), result.get(timeout=1)])
        self.assertEqual(("committed",), outcomes[0])
        self.assertEqual("rejected", outcomes[1][0])
        self.assertEqual("RuntimeError", outcomes[1][1])
        self.assertIn("stale revision: expected 1, current 2", outcomes[1][2])
        self.assertEqual(released, self.session.snapshot())
        revision, meta_json = self._authority_revision()
        self.assertEqual(2, revision)
        self.assertIn("mutated after verified release", meta_json)
        with sqlite3.connect(self.authority) as connection:
            events = connection.execute(
                "SELECT revision, kind FROM events WHERE task_id='AR-0001' ORDER BY revision"
            ).fetchall()
            results = connection.execute("SELECT COUNT(*) FROM command_results").fetchone()
        self.assertEqual([(1, "import"), (2, "update")], events)
        self.assertEqual((0,), results)


if __name__ == "__main__":
    unittest.main()
