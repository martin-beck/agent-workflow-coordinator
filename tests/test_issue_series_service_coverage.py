# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Focused fail-closed and recovery coverage for the issue-series service."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from tools import issue_series_service as service

REPOSITORY = "github.com/owner/product"
DIGEST = "sha256:" + "a" * 64


class ProjectionBackend:
    """Small durable-backend stand-in that exposes only projection reads."""

    def __init__(
        self, series: list[dict[str, object]], publications: list[dict[str, object]]
    ) -> None:
        self.series = series
        self.publications = publications

    def load_issue_series(self) -> list[dict[str, object]]:
        return self.series

    def load_issue_series_publications(self) -> list[dict[str, object]]:
        return self.publications

    def create_issue_series(self, *_args: object, **_kwargs: object) -> None:
        return None

    def load_tasks(self) -> list[object]:
        return []


class IssueSeriesServiceCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self._inject("Meta", dict)
        self._inject("SQLiteBackend", type("SQLiteBackend", (), {}))
        self._inject("LOCK_TIMEOUT_SECONDS", 1)
        self._inject("LOCK_POLL_SECONDS", 0)
        self._inject("repository_slug", lambda value: value.removeprefix("github.com/"))

    def _inject(self, name: str, value: object) -> None:
        replacement = patch.object(service, name, value, create=True)
        replacement.start()
        self.addCleanup(replacement.stop)

    def patch_projection_writes(self) -> patch:
        def atomic(path: Path, value: str) -> None:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")

        return patch.object(service, "atomic", side_effect=atomic, create=True)

    def test_sqlite_projections_replace_stale_files_and_are_normalized(self) -> None:
        receipt = {
            "series_identity": {"repository": REPOSITORY, "issue_number": 42},
            "manifest": {"schema_version": 1, "members": []},
        }
        key = "b" * 64
        publication = {"publication_key": key, "status": "published"}
        backend = ProjectionBackend([receipt], [publication])
        stale_series = self.root / "issue-series" / "stale.json"
        stale_publication = self.root / "issue-series-publications" / ("c" * 64 + ".json")
        stale_series.parent.mkdir()
        stale_publication.parent.mkdir()
        stale_series.write_text("stale", encoding="utf-8")
        stale_publication.write_text("stale", encoding="utf-8")
        with patch.object(service, "ROOT", self.root, create=True), self.patch_projection_writes():
            series_paths = service.write_sqlite_issue_series_projections(backend)  # type: ignore[arg-type]
            publication_paths = service.write_sqlite_issue_series_publication_projections(backend)  # type: ignore[arg-type]
        self.assertEqual(1, len(series_paths))
        self.assertEqual(1, len(publication_paths))
        self.assertFalse(stale_series.exists())
        self.assertFalse(stale_publication.exists())
        self.assertEqual(receipt["manifest"], json.loads(series_paths[0].read_text()))
        self.assertEqual(publication, json.loads(publication_paths[0].read_text()))

    def test_plan_and_git_receipts_reject_unsafe_or_unlinked_projections(self) -> None:
        manifest = self.root / "series.json"
        manifest.write_text("{}", encoding="utf-8")
        tasks = [(Path("task"), {"id": "AR-1", "status": "open", "task_revision": 1}, "")]
        with (
            patch.object(
                service,
                "project_binding",
                return_value={"product_repository": REPOSITORY},
                create=True,
            ),
            self.assertRaisesRegex(RuntimeError, "invalid issue-series manifest"),
        ):
            service.load_issue_series_plan(self.root / "missing.json", tasks)  # type: ignore[arg-type]
        with (
            patch.object(service, "ROOT", self.root, create=True),
            self.assertRaisesRegex(RuntimeError, "outside the local series directory"),
        ):
            service._load_git_issue_series_receipt("../series.json", [])  # type: ignore[arg-type]
        invalid = [(Path("task"), {"id": "AR-1", "issue_series": "wrong"}, "")]
        with self.assertRaisesRegex(RuntimeError, "invalid issue-series linkage"):
            service.git_issue_series_receipts(invalid)  # type: ignore[arg-type]

    def test_git_publication_receipts_require_a_matching_local_series(self) -> None:
        directory = self.root / "issue-series-publications"
        directory.mkdir()
        key = service.publication_key(REPOSITORY, 42, DIGEST)
        (directory / f"{key}.json").write_text(
            json.dumps({"publication_key": key, "manifest_digest": DIGEST, "status": "published"}),
            encoding="utf-8",
        )
        series = [
            {
                "series_identity": {"repository": REPOSITORY, "issue_number": 42},
                "manifest_digest": DIGEST,
            }
        ]
        with (
            patch.object(service, "ROOT", self.root, create=True),
            patch.object(service, "git_issue_series_receipts", return_value=series),
        ):
            receipts = service.git_issue_series_publication_receipts([])
        self.assertEqual("published", receipts[0]["status"])
        (directory / f"{key}.json").write_text("{}", encoding="utf-8")
        with (
            patch.object(service, "ROOT", self.root, create=True),
            patch.object(service, "git_issue_series_receipts", return_value=series),
            self.assertRaisesRegex(RuntimeError, "does not match"),
        ):
            service.git_issue_series_publication_receipts([])

    def test_publication_lock_times_out_then_releases_a_real_lockfile(self) -> None:
        lock = self.root / "runtime" / "coordinator.lock"
        lock.parent.mkdir(parents=True)
        lock.write_text("", encoding="utf-8")
        publication_locks = lock.parent / "issue-series-publications"
        publication_locks.mkdir()
        held = os.open(publication_locks / "key.lock", os.O_CREAT | os.O_RDWR)
        try:
            import fcntl

            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with (
                patch.object(service, "coordinator_lock_path", return_value=lock, create=True),
                patch.object(service, "LOCK_TIMEOUT_SECONDS", 0, create=True),
                patch.object(service, "LOCK_POLL_SECONDS", 0, create=True),
                patch.object(service, "LockTimeoutError", RuntimeError, create=True),
                self.assertRaisesRegex(RuntimeError, "LOCK_TIMEOUT"),
                service.issue_series_publication_locked("key"),
            ):
                pass
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
            os.close(held)
        with (
            patch.object(service, "coordinator_lock_path", return_value=lock, create=True),
            service.issue_series_publication_locked("key"),
        ):
            self.assertTrue((lock.parent / "issue-series-publications" / "key.lock").exists())

    def test_git_publication_record_conflict_and_resolution_recovery(self) -> None:
        key = "d" * 64
        record = {"publication_key": key, "manifest_digest": DIGEST, "status": "ambiguous"}
        final = {**record, "status": "published", "comment_id": "9"}
        commits: list[str] = []
        with (
            patch.object(service, "ROOT", self.root, create=True),
            self.patch_projection_writes(),
            patch.object(
                service,
                "commit",
                side_effect=lambda message, _paths: commits.append(message) or True,
                create=True,
            ),
            patch.object(service, "push_replica", create=True),
        ):
            service._git_record_issue_series_publication(record)  # type: ignore[arg-type]
            service._git_record_issue_series_publication(record)  # type: ignore[arg-type]
            service._git_resolve_issue_series_publication(final)  # type: ignore[arg-type]
            service._git_resolve_issue_series_publication(final)  # type: ignore[arg-type]
            with self.assertRaisesRegex(RuntimeError, "already final"):
                service._git_resolve_issue_series_publication({**final, "comment_id": "10"})  # type: ignore[arg-type]
        self.assertEqual(2, len(commits))

    def test_live_transport_fails_closed_and_recovers_a_prior_marker(self) -> None:
        key = service.publication_key(REPOSITORY, 42, DIGEST)
        with patch.object(service.shutil, "which", return_value=None):
            unavailable = service._live_existing_issue_series_comment(REPOSITORY, 42, key, DIGEST)
        self.assertEqual("deferred_unavailable", unavailable.status)
        config = self.root / "config.json"
        config.write_text(json.dumps({"github_repository": "other/project"}), encoding="utf-8")
        with (
            patch.object(service, "CONFIG", config, create=True),
            patch.object(
                service, "config", return_value={"github_repository": "other/project"}, create=True
            ),
        ):
            outcome = service._live_issue_series_publication(REPOSITORY, 42, DIGEST, "body")
        self.assertEqual("deferred_wrong_identity", outcome.status)
        found = subprocess.CompletedProcess([], 0, "77\n", "")
        with (
            patch.object(service.shutil, "which", return_value="gh"),
            patch.object(service.subprocess, "run", return_value=found),
        ):
            recovered = service._live_existing_issue_series_comment(REPOSITORY, 42, key, DIGEST)
        self.assertEqual(("already_published", "77"), (recovered.status, recovered.comment_id))

    def test_live_transport_and_receipt_helpers_reject_untrusted_states(self) -> None:
        key = service.publication_key(REPOSITORY, 42, DIGEST)
        with patch.object(service, "CONFIG", self.root / "missing", create=True):
            self.assertEqual(
                "deferred_unavailable",
                service._live_issue_series_publication(REPOSITORY, 42, DIGEST, "body").status,
            )
        config = self.root / "config.json"
        config.write_text("{}", encoding="utf-8")
        with (
            patch.object(service, "CONFIG", config, create=True),
            patch.object(
                service, "config", return_value={"github_repository": "owner/product"}, create=True
            ),
            patch.object(service, "_live_existing_issue_series_comment", return_value=None),
            patch.object(service.shutil, "which", return_value="gh"),
            patch.object(
                service.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 1, "", "HTTP 409"),
            ),
        ):
            self.assertEqual(
                "deferred_conflict",
                service._live_issue_series_publication(REPOSITORY, 42, DIGEST, "body").status,
            )
        with (
            patch.object(service.shutil, "which", return_value="gh"),
            patch.object(service.subprocess, "run", side_effect=OSError("unavailable")),
        ):
            self.assertEqual(
                "deferred_timeout",
                service._live_existing_issue_series_comment(REPOSITORY, 42, key, DIGEST).status,
            )
        with patch.object(service, "ROOT", self.root, create=True):
            with self.assertRaisesRegex(RuntimeError, "invalid issue-series publication key"):
                service.issue_series_publication_path("not-a-key")
            path = service.issue_series_publication_path(key)
            path.parent.mkdir()
            path.write_text(json.dumps({"manifest_digest": "sha256:" + "b" * 64}))
            with (
                patch.object(service, "storage_backend", return_value=object(), create=True),
                self.assertRaisesRegex(RuntimeError, "identity changed"),
            ):
                service._existing_issue_series_publication(REPOSITORY, 42, DIGEST)

    def test_service_dispatch_and_git_recovery_fail_closed(self) -> None:
        self.assertFalse(service.dispatch_cli(Namespace(cmd="snapshot")))
        commands = {
            "cmd_issue_series_plan": lambda _args: None,
            "cmd_issue_series_create": lambda _args: None,
            "cmd_issue_series_publish": lambda _args: None,
        }
        with self.assertRaisesRegex(RuntimeError, "unsupported issue-series action"):
            service.dispatch_cli(
                Namespace(cmd="issue-series", issue_series_action="unknown"), commands
            )
        key = "e" * 64
        record = {"publication_key": key, "manifest_digest": DIGEST, "status": "ambiguous"}
        with (
            patch.object(service, "ROOT", self.root, create=True),
            self.patch_projection_writes(),
            patch.object(service, "commit", return_value=False, create=True),
            patch.object(service, "push_replica", create=True),
            self.assertRaisesRegex(RuntimeError, "produced no commit"),
        ):
            service._git_record_issue_series_publication(record)  # type: ignore[arg-type]
        path = self.root / "issue-series-publications" / f"{key}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        with (
            patch.object(service, "ROOT", self.root, create=True),
            self.assertRaisesRegex(RuntimeError, "identity changed"),
        ):
            service._git_resolve_issue_series_publication(
                {**record, "manifest_digest": "sha256:" + "b" * 64}
            )  # type: ignore[arg-type]

    def test_command_and_persistence_failures_preserve_fail_closed_outcomes(self) -> None:
        with (
            patch.object(service, "load_issue_series_plan", return_value="receipt"),
            patch.object(service, "all_tasks", return_value=[], create=True),
            patch("builtins.print") as printed,
        ):
            service.cmd_issue_series_plan(Namespace(manifest=self.root / "series.json"))
        printed.assert_called_once_with("receipt")
        receipt = {
            "current_state_digest": DIGEST,
            "series_identity": {"repository": REPOSITORY, "issue_number": 42},
        }
        backend = ProjectionBackend([], [])
        with (
            patch.object(service, "mutating_sqlite_backend", return_value=backend, create=True),
            patch.object(service, "now", return_value="2026-10-12T00:00:00+00:00", create=True),
            patch.object(
                service, "write_sqlite_projections", side_effect=RuntimeError("export"), create=True
            ),
            patch.object(service, "StorageCommittedError", RuntimeError, create=True),
            self.assertRaisesRegex(RuntimeError, "SQLITE_COMMITTED_EXPORT_FAILED"),
        ):
            service.create_sqlite_issue_series(receipt, [], object())  # type: ignore[arg-type]
        with (
            patch.object(service, "locked", return_value=MagicMock(), create=True),
            patch.object(
                service, "backend_selection", return_value={"backend": "unknown"}, create=True
            ),
            self.assertRaisesRegex(RuntimeError, "BACKEND_CHANGED"),
        ):
            service.cmd_issue_series_create(
                Namespace(manifest=self.root / "series.json", expected_state_digest=DIGEST)
            )

    def test_git_and_live_publication_error_paths_are_idempotent(self) -> None:
        key = "f" * 64
        record = {"publication_key": key, "manifest_digest": DIGEST, "status": "ambiguous"}
        with (
            patch.object(service, "ROOT", self.root, create=True),
            self.patch_projection_writes(),
            patch.object(service, "commit", return_value=True, create=True),
            patch.object(service, "push_replica", create=True),
        ):
            service._git_record_issue_series_publication(record)  # type: ignore[arg-type]
            with self.assertRaisesRegex(RuntimeError, "conflicts"):
                service._git_record_issue_series_publication(
                    {**record, "status": "deferred_timeout"}
                )  # type: ignore[arg-type]
            missing = {"publication_key": "a" * 64, "manifest_digest": DIGEST}
            with self.assertRaisesRegex(RuntimeError, "reservation is missing"):
                service._git_resolve_issue_series_publication(missing)  # type: ignore[arg-type]
        config = self.root / "config.json"
        config.write_text("{}", encoding="utf-8")
        common = {
            "CONFIG": config,
            "config": lambda: {"github_repository": "owner/product"},
            "_live_existing_issue_series_comment": lambda *_args: None,
        }
        with (
            patch.multiple(service, create=True, **common),
            patch.object(service.shutil, "which", return_value=None),
        ):
            self.assertEqual(
                "deferred_unavailable",
                service._live_issue_series_publication(REPOSITORY, 42, DIGEST, "body").status,
            )
        with (
            patch.multiple(service, create=True, **common),
            patch.object(service.shutil, "which", return_value="gh"),
            patch.object(
                service.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "not-a-number", ""),
            ),
        ):
            self.assertEqual(
                "ambiguous",
                service._live_issue_series_publication(REPOSITORY, 42, DIGEST, "body").status,
            )
