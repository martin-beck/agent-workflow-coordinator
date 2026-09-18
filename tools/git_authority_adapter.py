# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Read-only Git authority evidence for the future scoped adapter."""

# The executable and arguments are fixed by this adapter's Git observation contract.
# ruff: noqa: S603, S607

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from tools.admission_lease import AdmissionLease, AdmissionRecheck
from tools.lifecycle_session import LifecycleSession, _issue
from tools.lock_domain_scope import LockDomainScope
from tools.rollback_evidence import BackupObservation


class GitAuthorityError(RuntimeError):
    """Git authority evidence is unavailable or a mutation was requested."""


@dataclass(frozen=True)
class GitRollbackSessionState:
    """Typed identity returned by a bound read-only Git session reread."""

    project_id: str
    authority_revision: str
    state_revision: int
    fencing_token: str
    fencing_owner: str
    durable_barrier_id: str
    git_head: str
    git_branch: str


@dataclass(frozen=True, init=False)
class GitBackupObservation:
    """Immutable verified Git backup/session evidence."""

    commit: str
    artifact_count: int
    session: GitRollbackSessionState

    def __init__(self) -> None:
        raise TypeError("Git backup observation must be created by the verifier")

    @property
    def has_provenance(self) -> bool:
        """Backend-neutral marker: only verifier-produced values are exposed."""
        return True

    @classmethod
    def _from_verified(
        cls, session: GitRollbackSessionState, result: Mapping[str, object]
    ) -> GitBackupObservation:
        if not isinstance(session, GitRollbackSessionState):
            raise GitAuthorityError("Git backup observation session is invalid")
        if (
            set(result) != {"commit", "verified", "artifact_count"}
            or result.get("verified") is not True
            or result.get("commit") != session.git_head
            or type(result.get("artifact_count")) is not int
            or cast(int, result.get("artifact_count")) < 1
        ):
            raise GitAuthorityError("Git backup verification result is invalid")
        observation = object.__new__(cls)
        object.__setattr__(observation, "commit", session.git_head)
        object.__setattr__(observation, "artifact_count", result["artifact_count"])
        object.__setattr__(observation, "session", session)
        return observation

    @classmethod
    def from_adapter(
        cls,
        adapter: GitAuthorityAdapter,
        session: GitRollbackSessionState,
        backup: Path,
    ) -> GitBackupObservation:
        """Verify the backup through the concrete adapter before creating evidence."""
        if not isinstance(adapter, GitAuthorityAdapter):
            raise GitAuthorityError("Git backup observation requires a concrete adapter")
        if not isinstance(backup, Path):
            raise GitAuthorityError("Git backup observation artifact path is invalid")
        result = adapter.verify_backup_artifact(backup)
        return cls._from_verified(session, result)


@dataclass(frozen=True)
class GitRollbackArtifactBinding:
    """Immutable Git backup path captured beneath the bound artifact root."""

    artifact_root: Path
    git_backup_root: Path

    @classmethod
    def bind(cls, context: Mapping[str, object]) -> GitRollbackArtifactBinding:
        root = context.get("artifact_root")
        backup = context.get("git_backup_root")
        if not isinstance(root, str) or not isinstance(backup, str):
            raise GitAuthorityError("Git rollback artifact binding is incomplete")
        root_path = Path(root).resolve()
        backup_path = Path(backup).resolve()
        try:
            backup_path.relative_to(root_path)
        except ValueError as error:
            raise GitAuthorityError(
                "Git rollback artifact path is outside artifact root"
            ) from error
        return cls(root_path, backup_path)


_SUPPORTED_PHASES = frozenset(
    {
        "discover",
        "preflight",
        "quiesce",
        "backup",
        "stage",
        "commit",
        "validate",
        "reopen",
        "rollback",
    }
)


class GitAuthorityAdapter:
    """Read-only Git evidence adapter; execute remains permanently disabled."""

    requires_bound_rollback = True
    bound_rollback_kind = "git"

    def __init__(self, repository: Path) -> None:
        resolved = repository.resolve()
        if not resolved.is_dir():
            raise GitAuthorityError("Git authority repository is unavailable")
        self._repository = resolved

    def lifecycle_session(self) -> LifecycleSession:
        """Return an opaque session bound to this adapter's repository."""
        return _issue(self, self._repository)

    def create_backup_bound(self, destination: Path, *, quiesced: bool) -> Path:
        """Create a Git backup using this adapter's owned lifecycle session."""
        from tools.git_backup import create_backup

        return create_backup(
            self._repository,
            destination,
            quiesced=quiesced,
            session=self.lifecycle_session(),
        )

    def restore_backup_bound(self, backup: Path, destination: Path) -> None:
        """Restore using this adapter's owned lifecycle session."""
        from tools.git_backup import restore_backup

        restore_backup(backup, destination, session=_issue(self, backup))

    @staticmethod
    def observe_backup_identity(
        backup: Path,
        manifest: Path,
        *,
        control_store_identity: str,
        control_store_revision: int,
    ) -> BackupObservation:
        """Read backup artifacts and CAS identity without authorizing restore."""
        return BackupObservation.from_artifacts(
            backup,
            manifest,
            control_store_identity=control_store_identity,
            control_store_revision=control_store_revision,
        )

    @staticmethod
    def verify_backup_artifact(backup: Path) -> dict[str, object]:
        """Run the complete read-only Git backup verifier."""
        from tools.git_backup import verify_backup

        return verify_backup(backup)

    def _git(self, *arguments: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(self._repository), *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GitAuthorityError("Git authority observation failed") from error
        if result.returncode != 0:
            raise GitAuthorityError("Git authority observation was rejected")
        return result.stdout.strip()

    @staticmethod
    def _context(context: Mapping[str, object]) -> dict[str, object]:
        required = {
            "schema_version",
            "backend",
            "project_id",
            "operation_id",
            "state_revision",
            "authority_revision",
            "fencing_token",
            "fencing_owner",
            "durable_barrier_id",
            "artifact_root",
            "source",
            "destination",
            "manifest",
            "selector_ref",
            "barrier_identity_digest",
            "target",
            "envelope_digest",
        }
        if set(context) != required or context.get("backend") != "git":
            raise GitAuthorityError("Git authority context is incomplete or mismatched")
        schema_version = context.get("schema_version")
        if type(schema_version) is not int or schema_version < 1:
            raise GitAuthorityError("Git authority context types are invalid")
        state_revision = context.get("state_revision")
        if type(state_revision) is not int or state_revision < 1:
            raise GitAuthorityError("Git authority context types are invalid")
        for field in required - {"schema_version", "state_revision"}:
            value = context.get(field)
            if type(value) is not str or not value:
                raise GitAuthorityError("Git authority context types are invalid")
        if context.get("target") not in {"new", "rollback"}:
            raise GitAuthorityError("Git authority context target is invalid")
        return dict(context)

    def snapshot(self, phase: str, context: Mapping[str, object]) -> dict[str, Any]:
        """Return identity-bound, read-only Git facts; never claim mutability evidence."""
        if type(phase) is not str or phase not in _SUPPORTED_PHASES:
            raise GitAuthorityError("Git authority phase is invalid")
        value = self._context(context)
        status = self._git("status", "--porcelain=v1", "--untracked-files=all")
        head = self._git("rev-parse", "--verify", "HEAD")
        branch = self._git("symbolic-ref", "--short", "-q", "HEAD")
        if not head or not branch or status:
            raise GitAuthorityError("Git authority is not clean and branch-bound")
        value.update(
            {
                "phase": phase,
                "backend_identity_verified": True,
                "git_head": head,
                "git_branch": branch,
                "git_clean": True,
                "mutates_authority": False,
            }
        )
        return value

    def snapshot_bound(  # noqa: C901
        self,
        phase: str,
        context: Mapping[str, object],
        scope: LockDomainScope,
        *,
        lease: AdmissionLease,
        admission_recheck: AdmissionRecheck,
        expected_branch: str,
        expected_head: str,
    ) -> dict[str, Any]:
        """Read Git identity only inside a trusted session scope.

        The scope performs the durable lease/revision reread immediately before
        observation.  This remains an evidence-only seam; no Git mutation is
        reachable from it.
        """
        from tools.scoped_backend_adapter import ScopedBackendAdapter

        if type(phase) is not str or phase not in _SUPPORTED_PHASES:
            raise GitAuthorityError("Git authority phase is invalid")
        if type(expected_branch) is not str or not expected_branch:
            raise GitAuthorityError("expected Git branch identity is invalid")
        if type(expected_head) is not str or not expected_head:
            raise GitAuthorityError("expected Git head identity is invalid")
        if not isinstance(lease, AdmissionLease):
            raise GitAuthorityError("trusted admission lease is required")
        if not isinstance(admission_recheck, AdmissionRecheck):
            raise GitAuthorityError("trusted admission recheck is required")
        if admission_recheck.lease != lease:
            raise GitAuthorityError("trusted admission recheck does not match lease")
        try:
            validated_context = self._context(context)
        except GitAuthorityError:
            raise
        except Exception as error:
            raise GitAuthorityError("Git authority context is invalid") from error
        if not isinstance(scope, LockDomainScope):
            raise GitAuthorityError("concrete lock-domain scope is required")
        expected_identity = {
            "project_id": lease.project_id,
            "authority_revision": lease.authority_revision,
            "fencing_token": lease.fencing_token,
            "fencing_owner": lease.fencing_owner,
            "durable_barrier_id": lease.durable_barrier_id,
            "state_revision": lease.revision,
        }
        if any(validated_context.get(name) != value for name, value in expected_identity.items()):
            raise GitAuthorityError("trusted session identity changed")
        try:
            value = ScopedBackendAdapter(self, scope).snapshot(
                phase, validated_context, scope_context=expected_identity
            )
        except GitAuthorityError:
            raise
        except (TypeError, RuntimeError) as error:
            raise GitAuthorityError("trusted Git session reread was rejected") from error
        expected_result_keys = set(validated_context) | {
            "phase",
            "backend_identity_verified",
            "git_head",
            "git_branch",
            "git_clean",
            "mutates_authority",
        }
        if set(value) != expected_result_keys:
            raise GitAuthorityError("Git authority backend result schema changed")
        for field, expected in validated_context.items():
            if value.get(field) != expected or type(value.get(field)) is not type(expected):
                raise GitAuthorityError("Git authority backend context identity changed")
        if type(value.get("phase")) is not str or value.get("phase") != phase:
            raise GitAuthorityError("Git authority backend phase changed")
        if (
            type(value.get("backend_identity_verified")) is not bool
            or value.get("backend_identity_verified") is not True
        ):
            raise GitAuthorityError("Git authority backend identity is unverified")
        if (
            type(value.get("mutates_authority")) is not bool
            or value.get("mutates_authority") is not False
        ):
            raise GitAuthorityError("Git authority backend is not read-only")
        if type(value.get("git_head")) is not str or not value.get("git_head"):
            raise GitAuthorityError("Git authority backend head is unverified")
        if type(value.get("git_branch")) is not str or not value.get("git_branch"):
            raise GitAuthorityError("Git authority backend branch is unverified")
        if type(value.get("git_clean")) is not bool or value.get("git_clean") is not True:
            raise GitAuthorityError("Git authority backend cleanliness is unverified")
        if value.get("git_branch") != expected_branch or value.get("git_head") != expected_head:
            raise GitAuthorityError("Git authority identity changed")
        return value

    def snapshot_bound_reread(
        self,
        context: Mapping[str, object],
        scope: LockDomainScope,
        *,
        lease: AdmissionLease,
        admission_recheck: AdmissionRecheck,
        expected_branch: str,
        expected_head: str,
    ) -> GitRollbackSessionState:
        """Return typed bound session identity without caller CAS fields."""
        value = self.snapshot_bound(
            "rollback",
            context,
            scope,
            lease=lease,
            admission_recheck=admission_recheck,
            expected_branch=expected_branch,
            expected_head=expected_head,
        )
        return GitRollbackSessionState(
            project_id=str(value["project_id"]),
            authority_revision=str(value["authority_revision"]),
            state_revision=int(value["state_revision"]),
            fencing_token=str(value["fencing_token"]),
            fencing_owner=str(value["fencing_owner"]),
            durable_barrier_id=str(value["durable_barrier_id"]),
            git_head=str(value["git_head"]),
            git_branch=str(value["git_branch"]),
        )

    def preflight_git(
        self,
        context: Mapping[str, object],
        scope: LockDomainScope,
        *,
        lease: AdmissionLease,
        admission_recheck: AdmissionRecheck,
        expected_branch: str,
        expected_head: str,
    ) -> GitBackupObservation:
        """Verify a bound Git backup as read-only rollback preflight evidence.

        Session identity is reread through the concrete scope before the
        verifier is invoked.  This method never calls ``execute`` or any
        restore/authorization path.
        """
        # ``git_backup_root`` is a Git-only artifact binding and is not part
        # of the shared authority context schema.
        context_without_artifact = dict(context)
        context_without_artifact.pop("git_backup_root", None)
        value = self._context(context_without_artifact)
        if value.get("target") != "rollback":
            raise GitAuthorityError("Git preflight requires rollback target")
        binding_context = dict(context)
        # The manifest is the canonical Git backup marker in the shared
        # PhaseContext; derive its containing backup directory rather than
        # accepting an independent caller-supplied CAS/path identity.
        binding_context.setdefault(
            "git_backup_root", str(Path(str(binding_context["manifest"])).parent)
        )
        binding = GitRollbackArtifactBinding.bind(binding_context)
        manifest = Path(str(value["manifest"])).resolve()
        canonical_manifest = (binding.git_backup_root / "manifest.json").resolve()
        if manifest != canonical_manifest or not manifest.is_file() or manifest.is_symlink():
            raise GitAuthorityError("Git rollback manifest is not bound to backup root")
        session = self.snapshot_bound_reread(
            value,
            scope,
            lease=lease,
            admission_recheck=admission_recheck,
            expected_branch=expected_branch,
            expected_head=expected_head,
        )
        try:
            return GitBackupObservation.from_adapter(self, session, binding.git_backup_root)
        except GitAuthorityError:
            raise
        except Exception as error:
            raise GitAuthorityError("Git rollback backup verification failed") from error

    def verify_rollback_context(self, context: Mapping[str, object]) -> dict[str, Any]:
        """Reread clean Git identity but do not authorize rollback."""
        if context.get("target") != "rollback":
            raise GitAuthorityError("Git rollback context target is invalid")
        value = self.snapshot("rollback", context)
        value["rollback_context_verified"] = False
        return value

    def verify_rollback_context_bound(
        self,
        context: Mapping[str, object],
        scope: LockDomainScope,
        *,
        lease: AdmissionLease,
        admission_recheck: AdmissionRecheck,
        expected_branch: str,
        expected_head: str,
    ) -> dict[str, Any]:
        """Reread rollback evidence only inside a trusted scope."""
        if context.get("target") != "rollback":
            raise GitAuthorityError("Git rollback context target is invalid")
        value = self.snapshot_bound(
            "rollback",
            context,
            scope,
            lease=lease,
            admission_recheck=admission_recheck,
            expected_branch=expected_branch,
            expected_head=expected_head,
        )
        value["rollback_context_verified"] = False
        return value

    def execute(self, _phase: str, _context: Mapping[str, object]) -> dict[str, Any]:
        raise GitAuthorityError("Git authority mutation adapter is not implemented")
