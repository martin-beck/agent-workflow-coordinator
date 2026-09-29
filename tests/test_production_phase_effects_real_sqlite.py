# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path

from tools.authority_neutral_commit import CommitAdmissionBundle
from tools.rollback_control_store import SQLiteBarrierSessionStore, SQLiteRollbackControlStore
from tools.sqlite_authority_adapter import SQLiteAuthorityAdapter
from tools.upgrade_identity import (
    BarrierChildIdentity,
    BarrierSessionIdentity,
    canonical_barrier_session_digest,
)


def _admission() -> CommitAdmissionBundle:
    return CommitAdmissionBundle(
        backend="sqlite",
        target="new",
        operation_id="real-sqlite-test:commit",
        fencing_token="fence-real",  # noqa: S106
        state_revision=3,
        barrier_id="barrier-real",
        artifact_identity="artifact-real",
        manifest_identity="manifest-real",
        selector_identity="selector-real",
        runtime_identity="runtime-real",
    )


class RealSQLitePhaseEffectsTests(unittest.TestCase):
    def test_real_sqlite_durable_commit_round_trip_and_reopen(self) -> None:
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
                    "attempt_id": "real-sqlite-attempt",
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
                    1,
                    BarrierChildIdentity.bind(identity, "real-sqlite-test:commit", "new"),
                )
                session.bind_child(
                    2,
                    BarrierChildIdentity.bind(identity, "real-sqlite-test:rollback", "rollback"),
                )
                admission = _admission()
                adapter = SQLiteAuthorityAdapter(database)

                def reread() -> dict[str, object]:
                    state = session.snapshot()
                    if state is None:
                        raise AssertionError("durable session disappeared")
                    return {
                        "backend": "sqlite",
                        "target": "new",
                        "operation_id": admission.operation_id,
                        "state_revision": state.revision,
                        "barrier_id": state.identity.durable_barrier_id,
                        "fencing_token": state.identity.fencing_token,
                        "artifact_identity": admission.artifact_identity,
                        "manifest_identity": admission.manifest_identity,
                        "selector_identity": admission.selector_identity,
                        "runtime_identity": admission.runtime_identity,
                    }

                capability = adapter.bind_durable_commit_capability(
                    admission,
                    session,
                    session_revision=3,
                    admission_reread=reread,
                )

                def update(connection: sqlite3.Connection) -> None:
                    connection.execute("UPDATE state SET value='new' WHERE id=1")

                receipt = capability.execute(update)
                self.assertTrue(receipt.mutates_authority)
                with closing(sqlite3.connect(database)) as check:
                    self.assertEqual(("new",), check.execute("SELECT value FROM state").fetchone())

                reopened = SQLiteRollbackControlStore(control.path, project, database)
                reopened_state = SQLiteBarrierSessionStore(reopened).snapshot()
                self.assertIsNotNone(reopened_state)
                assert reopened_state is not None
                self.assertEqual("held", reopened_state.status)
                with closing(sqlite3.connect(control.path)) as check:
                    outcomes = check.execute(
                        "SELECT operation_id, outcome FROM authority_effect_intent "
                        "WHERE project_id=?",
                        (project,),
                    ).fetchall()
                self.assertEqual([("real-sqlite-test:commit", "committed")], outcomes)


if __name__ == "__main__":
    unittest.main()
