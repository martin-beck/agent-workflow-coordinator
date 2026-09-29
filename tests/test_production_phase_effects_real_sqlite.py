# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.authority_neutral_commit import CommitAdmissionBundle
from tools.production_phase_effects import bind_production_phase_effects
from tools.production_phase_engine import ProductionPhaseBinding
from tools.rollback_control_store import SQLiteBarrierSessionStore, SQLiteRollbackControlStore
from tools.sqlite_authority_adapter import SQLiteAuthorityAdapter
from tools.upgrade_binding import LiveUpgradeBinding
from tools.upgrade_identity import (
    BarrierChildIdentity,
    BarrierSessionIdentity,
    canonical_barrier_session_digest,
)


def _admission(target: str) -> CommitAdmissionBundle:
    suffix = "commit" if target == "new" else "rollback"
    return CommitAdmissionBundle(
        backend="sqlite",
        target=target,
        operation_id=f"real-phase-test:{suffix}",
        fencing_token="fence-real",  # noqa: S106
        state_revision=3,
        barrier_id="barrier-real",
        artifact_identity="artifact-real",
        manifest_identity="manifest-real",
        selector_identity="selector-real",
        runtime_identity="runtime-real",
    )


def _reread(admission: CommitAdmissionBundle) -> dict[str, object]:
    return {field: getattr(admission, field) for field in admission.__dataclass_fields__}


def _operations() -> dict[str, dict[str, object]]:
    common = {
        "backend": "sqlite",
        "selector_ref": ".runtime/runtime-selector.json",
        "expected_state_revision": 3,
        "barrier_id": "barrier-real",
        "fencing_token": "fence-real",
        "backup_operation_id": "real-phase-test:backup",
    }

    def operation(phase: str, opcode: str, *, target: str | None = None) -> dict[str, object]:
        inputs = dict(common)
        if target is not None:
            inputs["target"] = target
        return {
            "operation_id": f"real-phase-test:{phase}",
            "opcode": opcode,
            "inputs": inputs,
            "timeout_seconds": 300,
            "resources": ["maintenance-barrier"],
            "preconditions": ["previous-phase-complete"],
            "postconditions": [f"{phase}-contract-satisfied"],
            "evidence": ["durable-operation-record"],
            "durable_record": "operation-id-and-outcome",
        }

    return {
        "commit": operation("commit", "authority.atomic_replace"),
        "rollback": operation("rollback", "backend.restore", target="rollback"),
    }


class RealSQLitePhaseEffectsTests(unittest.TestCase):
    def test_real_sqlite_commit_and_rollback_are_durably_journaled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "authority.sqlite"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    "CREATE TABLE state (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute("INSERT INTO state VALUES (1, 'old')")
            database.chmod(0o600)
            with closing(sqlite3.connect(database)) as keepalive:
                keepalive.execute("PRAGMA wal_autocheckpoint=0")
                keepalive.commit()

                project = str(uuid.uuid4())
                identity_record: dict[str, object] = {
                    "schema_version": 1,
                    "project_id": project,
                    "attempt_id": "real-phase-attempt",
                    "state_revision": 1,
                    "authority_revision_at_acquire": "authority-real",
                    "durable_barrier_id": "barrier-real",
                    "fencing_token": "fence-real",
                    "fencing_owner": "owner-real",
                    "identity_digest": "0" * 64,
                }
                identity_record["identity_digest"] = canonical_barrier_session_digest(
                    identity_record
                )
                identity = BarrierSessionIdentity.from_record(identity_record)
                control = SQLiteRollbackControlStore(root / "control.sqlite", project, database)
                session = SQLiteBarrierSessionStore(control, lambda: "authority-real")
                session.create(identity)
                session.bind_child(
                    1, BarrierChildIdentity.bind(identity, "real-phase-test:commit", "new")
                )
                session.bind_child(
                    2,
                    BarrierChildIdentity.bind(identity, "real-phase-test:rollback", "rollback"),
                )
                adapter = SQLiteAuthorityAdapter(database)
                runtime = SimpleNamespace(
                    runtime_envelope={
                        "backend": "sqlite",
                        "operation_id": "real-phase-test",
                        "state_revision": 3,
                        "durable_barrier_id": "barrier-real",
                        "fencing_token": "fence-real",
                        "target": "rollback",
                    }
                )
                live = object.__new__(LiveUpgradeBinding)
                for field, value in {
                    "runtime": runtime,
                    "adapter": adapter,
                    "scope": object(),
                    "lease": object(),
                    "admission_recheck": object(),
                    "expected_branch": None,
                    "expected_head": None,
                    "expected_git_repository": None,
                    "session": object(),
                    "_token": object(),
                }.items():
                    object.__setattr__(live, field, value)
                phase_binding = object.__new__(ProductionPhaseBinding)
                object.__setattr__(phase_binding, "operations", _operations())
                object.__setattr__(phase_binding, "live_binding", live)

                commit = _admission("new")
                rollback = _admission("rollback")

                def rollback_effect(_argument: object) -> dict[str, object]:
                    with closing(sqlite3.connect(database)) as connection, connection:
                        connection.execute("UPDATE state SET value='old' WHERE id=1")
                    return {
                        **_reread(rollback),
                        "mutates_authority": True,
                    }

                with patch.object(LiveUpgradeBinding, "is_admitted", return_value=True):
                    effects = bind_production_phase_effects(
                        phase_binding,
                        session,
                        commit,
                        rollback,
                        session_revision=3,
                        admission_reread=lambda: _reread(commit),
                        commit_argument=lambda connection: connection.execute(
                            "UPDATE state SET value='new' WHERE id=1"
                        ),
                        rollback_argument="restore-old",
                        rollback_effect=rollback_effect,
                    )
                    effects.commit.execute(
                        "commit",
                        {
                            "backend": "sqlite",
                            "target": "new",
                            "operation_id": "real-phase-test:commit",
                            "state_revision": 3,
                            "durable_barrier_id": "barrier-real",
                            "fencing_token": "fence-real",
                        },
                    )
                    with closing(sqlite3.connect(database)) as check:
                        self.assertEqual(
                            ("new",), check.execute("SELECT value FROM state").fetchone()
                        )
                    effects.rollback.execute(
                        {
                            "backend": "sqlite",
                            "target": "rollback",
                            "selector_ref": ".runtime/runtime-selector.json",
                            "operation_id": "real-phase-test:rollback",
                            "state_revision": 3,
                            "durable_barrier_id": "barrier-real",
                            "fencing_token": "fence-real",
                        }
                    )
                with closing(sqlite3.connect(database)) as check:
                    self.assertEqual(("old",), check.execute("SELECT value FROM state").fetchone())
                with closing(sqlite3.connect(control.path)) as check:
                    outcomes = check.execute(
                        "SELECT operation_id, outcome FROM authority_effect_intent "
                        "WHERE project_id=? ORDER BY rowid",
                        (project,),
                    ).fetchall()
                self.assertEqual(
                    [
                        ("real-phase-test:commit", "committed"),
                        ("real-phase-test:rollback", "committed"),
                    ],
                    outcomes,
                )


if __name__ == "__main__":
    unittest.main()
