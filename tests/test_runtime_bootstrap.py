# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Hostile tests for stable runtime selector resolution."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from typing import cast
from unittest.mock import patch

from tools.runtime_bootstrap import (
    DispatchAdmission,
    ExpectedRuntimeIdentity,
    ResolvedRuntime,
    VerifiedManifest,
    read_runtime_manifest,
    resolve_selected_runtime,
    resolve_selected_runtime_bound,
    verify_runtime_manifest,
)
from tools.upgrade_authority import AuthorityError, commit_runtime_selector, read_runtime_selector


class RuntimeBootstrapTests(unittest.TestCase):
    @staticmethod
    def _write_manifest(runtime: Path, release: str = "v1.2.3") -> None:
        runtime.joinpath("runtime-manifest.json").write_text(
            json.dumps(
                {
                    "release": release,
                    "source_commit": "a" * 40,
                    "tag_ref": f"refs/tags/{release}",
                    "tag_object": "b" * 40,
                    "signature_sha256": "c" * 64,
                    "trust_policy_sha256": "d" * 64,
                    "vendor_manifest_sha256": "e" * 64,
                },
                separators=(",", ":"),
            )
        )
        runtime.joinpath("runtime-manifest.json").chmod(0o600)

    def test_resolves_owner_only_versioned_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            releases.chmod(0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            selected.chmod(0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            expected = self._identity(selected)
            self.assertEqual(
                selected,
                resolve_selected_runtime(selector, releases, expected, self._verifier),
            )

    def test_rejects_selector_hard_link_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            selector = Path(directory) / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            alias = Path(directory) / "selector-alias.json"
            os.link(selector, alias)
            with self.assertRaisesRegex(AuthorityError, "private regular file"):
                read_runtime_selector(selector)

    def test_bound_runtime_rejects_release_directory_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                selected.rename(releases / "v1.2.3.old")
                replacement = releases / "v1.2.3"
                replacement.mkdir(mode=0o700)
                self._write_manifest(replacement)
                with self.assertRaisesRegex(AuthorityError, "identity changed"):
                    resolved.revalidate()
                resolved.close()
            with self.assertRaisesRegex(AuthorityError, "unavailable"):
                resolved.revalidate()

    def test_bound_runtime_revalidates_manifest_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                admission = resolved.admit_for_dispatch()
                self.assertIs(admission.runtime, resolved)
                self.assertEqual(resolved.identity, admission.identity)
                admission.revalidate()
                manifest = selected / "runtime-manifest.json"
                replacement = selected / "replacement.json"
                replacement.write_text(manifest.read_text().replace("v1.2.3", "v1.2.4"))
                replacement.chmod(0o600)
                manifest.unlink()
                replacement.rename(manifest)
                with self.assertRaisesRegex(AuthorityError, "manifest identity changed"):
                    resolved.admit_for_dispatch()
                self.assertEqual(-1, resolved.descriptor)
                with self.assertRaisesRegex(AuthorityError, "resolved runtime is unavailable"):
                    admission.revalidate()

    def test_dispatch_rejects_same_content_manifest_swap_after_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                manifest = selected / "runtime-manifest.json"
                replacement = selected / "replacement.json"
                replacement.write_bytes(manifest.read_bytes())
                replacement.chmod(0o600)
                manifest.unlink()
                replacement.rename(manifest)
                with self.assertRaisesRegex(AuthorityError, "identity changed"):
                    resolved.admit_for_dispatch()

    def test_dispatch_rejects_selector_parent_swap_after_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector_parent = root / "selector"
            selector_parent.mkdir(mode=0o700)
            selector = selector_parent / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                moved = root / "selector-original"
                selector_parent.rename(moved)
                selector_parent.symlink_to(moved, target_is_directory=True)
                with self.assertRaisesRegex(AuthorityError, "selector"):
                    resolved.admit_for_dispatch()

    def test_dispatch_rejects_malformed_retained_manifest_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                object.__setattr__(resolved, "_manifest_file_identity", (1, object()))
                with self.assertRaisesRegex(AuthorityError, "identity is unavailable"):
                    resolved.revalidate_manifest()

    def test_bound_runtime_rejects_ancestor_symlink_after_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                moved = root / "releases-original"
                releases.rename(moved)
                releases.symlink_to(moved, target_is_directory=True)
                with self.assertRaisesRegex(AuthorityError, "ancestor changed"):
                    resolved.revalidate()

    def test_dispatch_admission_context_closes_retained_handle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                admission = resolved.admit_for_dispatch()
                with admission:
                    admission.validate_identity(resolved.identity)
                self.assertEqual(-1, resolved.descriptor)

    def test_dispatch_admission_context_closes_on_consumer_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                admission = resolved.admit_for_dispatch()
                with self.assertRaisesRegex(RuntimeError, "consumer failed"), admission:
                    raise RuntimeError("consumer failed")
                self.assertEqual(-1, resolved.descriptor)

    def test_dispatch_admission_context_closes_on_revalidation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                admission = resolved.admit_for_dispatch()
                manifest = selected / "runtime-manifest.json"
                replacement = selected / "replacement.json"
                replacement.write_text(manifest.read_text().replace("v1.2.3", "v1.2.4"))
                replacement.chmod(0o600)
                manifest.unlink()
                replacement.rename(manifest)
                with self.assertRaisesRegex(AuthorityError, "manifest identity changed"), admission:
                    admission.revalidate()
                self.assertEqual(-1, resolved.descriptor)

    def test_dispatch_admission_rejects_cross_bound_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                admission = resolved.admit_for_dispatch()
                wrong = VerifiedManifest(
                    "v1.2.4", resolved.identity.identity, resolved.identity.digest
                )
                with self.assertRaisesRegex(AuthorityError, "identity is not bound"):
                    admission.validate_identity(wrong)
                self.assertEqual(-1, resolved.descriptor)

    def test_dispatch_admission_rejects_selector_replacement_after_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                commit_runtime_selector(selector, "v1.2.3", "v1.2.4")
                with self.assertRaisesRegex(AuthorityError, "selector changed"):
                    resolved.admit_for_dispatch()

    def test_dispatch_admission_rejects_forged_constructor_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                forged = VerifiedManifest(
                    resolved.identity.release,
                    resolved.identity.identity,
                    resolved.identity.digest,
                )
                with self.assertRaisesRegex(AuthorityError, "identity is not bound"):
                    DispatchAdmission(resolved, forged)
                self.assertEqual(-1, resolved.descriptor)

    def test_dispatch_admission_rejects_malformed_constructor_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                with self.assertRaisesRegex(AuthorityError, "identity is not bound"):
                    DispatchAdmission(resolved, object())  # type: ignore[arg-type]
                self.assertEqual(-1, resolved.descriptor)

    def test_dispatch_admission_rejects_malformed_nested_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                malformed = VerifiedManifest(
                    resolved.identity.release,
                    cast(ExpectedRuntimeIdentity, object()),
                    resolved.identity.digest,
                )
                object.__setattr__(resolved, "identity", malformed)
                with self.assertRaisesRegex(AuthorityError, "identity is not bound"):
                    DispatchAdmission(resolved, malformed)
                self.assertEqual(-1, resolved.descriptor)

    def test_dispatch_admission_rejects_malformed_manifest_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                malformed = VerifiedManifest(
                    "not-a-release", resolved.identity.identity, "not-a-digest"
                )
                object.__setattr__(resolved, "identity", malformed)
                with self.assertRaisesRegex(AuthorityError, "identity is not bound"):
                    DispatchAdmission(resolved, malformed)
                self.assertEqual(-1, resolved.descriptor)

    def test_dispatch_admission_rejects_identity_replacement_before_revalidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                admission = resolved.admit_for_dispatch()
                resolved.identity = VerifiedManifest(
                    resolved.identity.release,
                    resolved.identity.identity,
                    resolved.identity.digest,
                )
                with self.assertRaisesRegex(AuthorityError, "identity is not bound"):
                    admission.revalidate()
                self.assertEqual(-1, resolved.descriptor)

    def test_dispatch_admission_revalidate_rejects_malformed_runtime(self) -> None:
        admission = object.__new__(DispatchAdmission)
        object.__setattr__(admission, "runtime", object())
        object.__setattr__(admission, "identity", object())
        with self.assertRaisesRegex(AuthorityError, "runtime is not retained"):
            admission.revalidate()

    def test_resolved_runtime_revalidate_manifest_rejects_malformed_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                object.__setattr__(resolved, "identity", object())
                with self.assertRaisesRegex(AuthorityError, "identity is unavailable"):
                    resolved.revalidate_manifest()

    def test_resolved_runtime_revalidate_rejects_malformed_nested_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                resolved.identity = VerifiedManifest(
                    resolved.identity.release,
                    cast(ExpectedRuntimeIdentity, object()),
                    resolved.identity.digest,
                )
                with self.assertRaisesRegex(AuthorityError, "identity is unavailable"):
                    resolved.revalidate_manifest()

    def test_resolved_runtime_revalidate_rejects_malformed_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                resolved.identity = VerifiedManifest(
                    resolved.identity.release,
                    resolved.identity.identity,
                    object(),  # type: ignore[arg-type]
                )
                with self.assertRaisesRegex(AuthorityError, "digest is unavailable"):
                    resolved.revalidate_manifest()

    def test_resolved_runtime_revalidate_rejects_malformed_identity_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                identity = resolved.identity.identity
                object.__setattr__(identity, "source_commit", object())
                with self.assertRaisesRegex(AuthorityError, "identity is unavailable"):
                    resolved.revalidate_manifest()

    def test_resolved_runtime_revalidate_rejects_malformed_digest_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                resolved.identity = VerifiedManifest(
                    resolved.identity.release,
                    resolved.identity.identity,
                    "g" * 64,
                )
                with self.assertRaisesRegex(AuthorityError, "digest is unavailable"):
                    resolved.revalidate_manifest()

    def test_resolved_runtime_revalidate_rejects_malformed_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                resolved.identity = VerifiedManifest(
                    cast(str, object()),
                    resolved.identity.identity,
                    resolved.identity.digest,
                )
                with self.assertRaisesRegex(AuthorityError, "release is unavailable"):
                    resolved.revalidate_manifest()

    def test_resolved_runtime_revalidate_rejects_malformed_identity_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                identity = resolved.identity.identity
                object.__setattr__(identity, "source_commit", "a")
                with self.assertRaisesRegex(AuthorityError, "identity is unavailable"):
                    resolved.revalidate_manifest()

    def test_dispatch_admission_close_rejects_malformed_runtime(self) -> None:
        admission = object.__new__(DispatchAdmission)
        object.__setattr__(admission, "runtime", object())
        object.__setattr__(admission, "identity", object())
        with self.assertRaisesRegex(AuthorityError, "runtime is not retained"):
            admission.close()

    def test_resolved_runtime_close_tolerates_malformed_descriptor(self) -> None:
        runtime = object.__new__(ResolvedRuntime)
        object.__setattr__(runtime, "descriptor", object())
        runtime.close()
        self.assertEqual(-1, runtime.descriptor)

    def test_resolved_runtime_revalidate_rejects_malformed_path(self) -> None:
        runtime = object.__new__(ResolvedRuntime)
        object.__setattr__(runtime, "path", object())
        with self.assertRaisesRegex(AuthorityError, "path is unavailable"):
            runtime.revalidate()

    def test_resolved_runtime_revalidate_rejects_relative_path(self) -> None:
        runtime = object.__new__(ResolvedRuntime)
        object.__setattr__(runtime, "path", Path("relative-runtime"))
        object.__setattr__(runtime, "descriptor", 0)
        with self.assertRaisesRegex(AuthorityError, "path is unavailable"):
            runtime.revalidate()

    def test_resolved_runtime_revalidate_rejects_malformed_descriptor(self) -> None:
        runtime = object.__new__(ResolvedRuntime)
        object.__setattr__(runtime, "path", Path())
        object.__setattr__(runtime, "descriptor", object())
        with self.assertRaisesRegex(AuthorityError, "descriptor is unavailable"):
            runtime.revalidate()

    def test_resolved_runtime_revalidate_rejects_malformed_directory_identity(self) -> None:
        runtime = object.__new__(ResolvedRuntime)
        object.__setattr__(runtime, "path", Path.cwd())
        object.__setattr__(runtime, "descriptor", 0)
        object.__setattr__(runtime, "_directory_identity", (1, 2, 3, 4, object()))
        with self.assertRaisesRegex(AuthorityError, "identity is unavailable"):
            runtime.revalidate()

    def test_resolved_runtime_revalidate_rejects_retained_non_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "not-a-directory"
            path.write_text("not a runtime")
            descriptor = os.open(path, os.O_RDONLY)
            try:
                value = os.fstat(descriptor)
                identity = (
                    value.st_dev,
                    value.st_ino,
                    stat.S_IMODE(value.st_mode),
                    value.st_uid,
                    value.st_nlink,
                )
                runtime = object.__new__(ResolvedRuntime)
                object.__setattr__(runtime, "path", path)
                object.__setattr__(runtime, "descriptor", descriptor)
                object.__setattr__(runtime, "_directory_identity", identity)
                with self.assertRaisesRegex(AuthorityError, "identity changed"):
                    runtime.revalidate()
            finally:
                os.close(descriptor)

    def test_dispatch_admission_rejects_malformed_identity_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                admission = resolved.admit_for_dispatch()
                with self.assertRaisesRegex(AuthorityError, "identity is not bound"):
                    admission.validate_identity(object())  # type: ignore[arg-type]
                self.assertEqual(-1, resolved.descriptor)

    def test_dispatch_admission_rejects_equality_spoof_identity(self) -> None:
        class EqualitySpoof:
            def __eq__(self, _other: object) -> bool:
                return True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                admission = resolved.admit_for_dispatch()
                with self.assertRaisesRegex(AuthorityError, "identity is not bound"):
                    admission.validate_identity(EqualitySpoof())  # type: ignore[arg-type]
                self.assertEqual(-1, resolved.descriptor)

    def test_dispatch_admission_rejects_equal_but_distinct_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                admission = resolved.admit_for_dispatch()
                equivalent = VerifiedManifest(
                    admission.identity.release,
                    admission.identity.identity,
                    admission.identity.digest,
                )
                with self.assertRaisesRegex(AuthorityError, "identity is not bound"):
                    admission.validate_identity(equivalent)
                self.assertEqual(-1, resolved.descriptor)

    def test_dispatch_admission_close_is_terminal_and_reuse_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                admission = resolved.admit_for_dispatch()
                admission.close()
                admission.close()
                self.assertEqual(-1, admission.runtime.descriptor)
                with self.assertRaisesRegex(AuthorityError, "resolved runtime is unavailable"):
                    admission.revalidate()

    def test_dispatch_admission_close_tolerates_external_descriptor_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with resolve_selected_runtime_bound(
                selector, releases, self._identity_for_release(), self._verifier
            ) as resolved:
                admission = resolved.admit_for_dispatch()
                os.close(resolved.descriptor)
                admission.close()
                admission.close()
                self.assertEqual(-1, resolved.descriptor)

    def test_bound_runtime_rejects_invalid_evidence_and_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with self.assertRaisesRegex(AuthorityError, "not bound"):
                resolve_selected_runtime_bound(
                    selector,
                    releases,
                    self._identity_for_release(),
                    lambda *_: True,  # type: ignore[arg-type]
                )
            with self.assertRaisesRegex(AuthorityError, "expected identity"):
                resolve_selected_runtime_bound(
                    selector,
                    releases,
                    ExpectedRuntimeIdentity(
                        "f" * 40,
                        "refs/tags/v1.2.3",
                        "b" * 40,
                        "c" * 64,
                        "d" * 64,
                        "e" * 64,
                    ),
                    self._verifier,
                )

    def test_bound_runtime_rejects_selector_and_release_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            selector.write_text(
                '{"schema_version":1,"active_release":"v1.2.3-extra","previous_release":"v1.2.2"}'
            )
            with self.assertRaisesRegex(AuthorityError, "selector release identity"):
                resolve_selected_runtime_bound(
                    selector, releases, self._identity_for_release(), self._verifier
                )
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            selected.chmod(0o755)
            with self.assertRaisesRegex(AuthorityError, "release is unsafe"):
                resolve_selected_runtime_bound(
                    selector, releases, self._identity_for_release(), self._verifier
                )

    def test_bound_runtime_closes_descriptor_on_verifier_oserror(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")

            def fail(_path: Path, _expected: ExpectedRuntimeIdentity) -> VerifiedManifest:
                raise OSError("verifier unavailable")

            with self.assertRaisesRegex(AuthorityError, "resolved runtime is unavailable"):
                resolve_selected_runtime_bound(
                    selector, releases, self._identity_for_release(), fail
                )

    def test_bound_runtime_wraps_unexpected_verifier_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")

            def fail(_path: Path, _expected: ExpectedRuntimeIdentity) -> VerifiedManifest:
                raise RuntimeError("unexpected verifier failure")

            with self.assertRaisesRegex(AuthorityError, "verification failed"):
                resolve_selected_runtime_bound(
                    selector, releases, self._identity_for_release(), fail
                )

    def test_bound_runtime_closes_descriptor_on_unexpected_verification_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")

            with (
                patch(
                    "tools.runtime_bootstrap.verify_runtime_manifest",
                    side_effect=RuntimeError("unexpected verification failure"),
                ),
                self.assertRaisesRegex(AuthorityError, "verification failed"),
            ):
                resolve_selected_runtime_bound(
                    selector, releases, self._identity_for_release(), self._verifier
                )

    def test_rejects_missing_symlink_and_unbounded_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            releases.chmod(0o700)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with self.assertRaisesRegex(AuthorityError, "unavailable"):
                resolve_selected_runtime(
                    selector, releases, self._identity_for_release(), self._verifier
                )
            (releases / "v1.2.3").symlink_to(root)
            with self.assertRaisesRegex(AuthorityError, "unsafe"):
                resolve_selected_runtime(
                    selector, releases, self._identity_for_release(), self._verifier
                )
            selector.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "active_release": "v1.2.3-extra",
                        "previous_release": "v1.2.2",
                    }
                )
            )
            with self.assertRaisesRegex(AuthorityError, "identity"):
                resolve_selected_runtime(
                    selector, releases, self._identity_for_release(), self._verifier
                )

    def test_requires_authenticity_verifier_and_rejects_failed_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            releases.chmod(0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            selected.chmod(0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with self.assertRaisesRegex(
                AuthorityError, "verifier and expected identity are required"
            ):
                resolve_selected_runtime(selector, releases, self._identity_for_release())
            with self.assertRaisesRegex(AuthorityError, "verification failed"):
                resolve_selected_runtime(
                    selector,
                    releases,
                    self._identity_for_release(),
                    lambda *_: (_ for _ in ()).throw(RuntimeError("no")),
                )

    def test_rejects_cross_bound_verified_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            foreign = ExpectedRuntimeIdentity(
                "f" * 40, "refs/tags/v1.2.3", "b" * 40, "c" * 64, "d" * 64, "e" * 64
            )
            with self.assertRaisesRegex(AuthorityError, "not bound"):
                resolve_selected_runtime(
                    selector,
                    releases,
                    self._identity_for_release(),
                    lambda path, _expected: VerifiedManifest(
                        "v1.2.3",
                        foreign,
                        sha256(path.joinpath("runtime-manifest.json").read_bytes()).hexdigest(),
                    ),
                )

    def test_rejects_equal_but_distinct_verified_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")

            def verifier(path: Path, expected: ExpectedRuntimeIdentity) -> VerifiedManifest:
                equivalent = ExpectedRuntimeIdentity(
                    expected.source_commit,
                    expected.tag_ref,
                    expected.tag_object,
                    expected.signature_sha256,
                    expected.trust_policy_sha256,
                    expected.vendor_manifest_sha256,
                )
                digest = sha256(path.joinpath("runtime-manifest.json").read_bytes()).hexdigest()
                return VerifiedManifest("v1.2.3", equivalent, digest)

            with self.assertRaisesRegex(AuthorityError, "not bound"):
                resolve_selected_runtime(selector, releases, self._identity_for_release(), verifier)

    def test_rejects_selector_manifest_release_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected, "v1.2.4")
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            with self.assertRaisesRegex(AuthorityError, "does not match selector"):
                resolve_selected_runtime(
                    selector, releases, self._identity_for_release(), self._verifier
                )

    def test_rejects_manifest_identity_not_matching_expected_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            foreign = ExpectedRuntimeIdentity(
                "f" * 40, "refs/tags/v1.2.3", "b" * 40, "c" * 64, "d" * 64, "e" * 64
            )
            with self.assertRaisesRegex(AuthorityError, "does not match expected identity"):
                resolve_selected_runtime(selector, releases, foreign, self._verifier)

    def test_rejects_manifest_replacement_after_authenticity_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")

            def replace(path: Path, expected: ExpectedRuntimeIdentity) -> VerifiedManifest:
                digest = sha256(path.joinpath("runtime-manifest.json").read_bytes()).hexdigest()
                replacement = path.joinpath("replacement.json")
                replacement.write_text(
                    path.joinpath("runtime-manifest.json").read_text().replace("v1.2.3", "v1.2.4")
                )
                replacement.chmod(0o600)
                path.joinpath("runtime-manifest.json").unlink()
                replacement.rename(path.joinpath("runtime-manifest.json"))
                return VerifiedManifest("v1.2.3", expected, digest)

            with self.assertRaisesRegex(AuthorityError, "identity changed|does not match"):
                resolve_selected_runtime(selector, releases, self._identity_for_release(), replace)

    def test_rejects_forged_vendor_identity_replacement_after_authenticity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")

            def replace(path: Path, expected: ExpectedRuntimeIdentity) -> VerifiedManifest:
                manifest = path / "runtime-manifest.json"
                value = json.loads(manifest.read_text())
                value["vendor_manifest_sha256"] = "f" * 64
                manifest.write_text(json.dumps(value, separators=(",", ":")))
                manifest.chmod(0o600)
                digest = sha256(manifest.read_bytes()).hexdigest()
                return VerifiedManifest("v1.2.3", expected, digest)

            with self.assertRaisesRegex(AuthorityError, "does not match"):
                resolve_selected_runtime(selector, releases, self._identity_for_release(), replace)

    def test_rejects_same_content_manifest_replacement_after_authenticity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            selected = releases / "v1.2.3"
            selected.mkdir(mode=0o700)
            self._write_manifest(selected)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")

            def replace(path: Path, expected: ExpectedRuntimeIdentity) -> VerifiedManifest:
                manifest = path / "runtime-manifest.json"
                replacement = path / "replacement.json"
                replacement.write_bytes(manifest.read_bytes())
                replacement.chmod(0o600)
                manifest.unlink()
                replacement.rename(manifest)
                digest = sha256(manifest.read_bytes()).hexdigest()
                return VerifiedManifest("v1.2.3", expected, digest)

            with self.assertRaisesRegex(AuthorityError, "identity changed"):
                resolve_selected_runtime(selector, releases, self._identity_for_release(), replace)

    def test_rejects_selector_replacement_during_authenticity_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir(mode=0o700)
            releases.chmod(0o700)
            for release in ("v1.2.3", "v1.2.4"):
                selected = releases / release
                selected.mkdir(mode=0o700)
                selected.chmod(0o700)
                self._write_manifest(selected, release)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")

            def replace_selector(path: Path, expected: ExpectedRuntimeIdentity) -> VerifiedManifest:
                commit_runtime_selector(selector, "v1.2.4", "v1.2.3")
                return self._verifier(path, expected)

            with self.assertRaisesRegex(AuthorityError, "changed during validation"):
                resolve_selected_runtime(
                    selector, releases, self._identity_for_release(), replace_selector
                )

    def test_manifest_verifier_requires_exact_owner_only_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            manifest = runtime / "runtime-manifest.json"
            manifest.write_text('{"release":"v1.2.3"}\n')
            manifest.chmod(0o600)
            digest = sha256(manifest.read_bytes()).hexdigest()
            self.assertTrue(verify_runtime_manifest(runtime, digest))
            with self.assertRaisesRegex(AuthorityError, "does not match"):
                verify_runtime_manifest(runtime, "0" * 64)
            with self.assertRaisesRegex(AuthorityError, "digest is invalid"):
                verify_runtime_manifest(runtime, "not-a-digest")

    def test_manifest_verifier_rejects_preopen_symlink_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            manifest = runtime / "runtime-manifest.json"
            manifest.write_text('{"release":"v1.2.3"}\n')
            manifest.chmod(0o600)
            digest = sha256(manifest.read_bytes()).hexdigest()
            original_open = os.open

            def replace_then_open(path: str | Path, flags: int) -> int:
                manifest.unlink()
                manifest.symlink_to(runtime / "other.json")
                return original_open(path, flags)

            with (
                patch("tools.runtime_bootstrap.os.open", side_effect=replace_then_open),
                self.assertRaisesRegex(AuthorityError, "unavailable"),
            ):
                verify_runtime_manifest(runtime, digest)

    def test_manifest_reader_rejects_duplicate_json_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            manifest = runtime / "runtime-manifest.json"
            manifest.write_text('{"release":"v1.2.3","release":"v1.2.3"}')
            manifest.chmod(0o600)
            with self.assertRaisesRegex(AuthorityError, "duplicate"):
                read_runtime_manifest(runtime)

    def test_manifest_reader_rejects_noncanonical_json_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            self._write_manifest(runtime)
            manifest = runtime / "runtime-manifest.json"
            manifest.write_text(json.dumps(json.loads(manifest.read_text()), indent=2))
            manifest.chmod(0o600)
            with self.assertRaisesRegex(AuthorityError, "not canonical"):
                read_runtime_manifest(runtime)

    def test_manifest_reader_rejects_invalid_json_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            manifest = runtime / "runtime-manifest.json"
            manifest.touch()
            manifest.chmod(0o600)
            for content, message in (("{", "JSON is invalid"), ('{"release":"v1.2.3"}', "fields")):
                manifest.write_text(content)
                with self.assertRaisesRegex(AuthorityError, message):
                    read_runtime_manifest(runtime)

    def test_manifest_reader_rejects_non_string_and_invalid_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            manifest = runtime / "runtime-manifest.json"
            manifest.touch()
            manifest.chmod(0o600)
            value = {
                "release": "v1.2.3",
                "source_commit": 1,
                "tag_ref": "refs/tags/v1.2.3",
                "tag_object": "b" * 40,
                "signature_sha256": "c" * 64,
                "trust_policy_sha256": "d" * 64,
                "vendor_manifest_sha256": "e" * 64,
            }
            manifest.write_text(json.dumps(value))
            with self.assertRaisesRegex(AuthorityError, "identity is invalid"):
                read_runtime_manifest(runtime)
            value["source_commit"] = "z" * 40
            manifest.write_text(json.dumps(value))
            with self.assertRaisesRegex(AuthorityError, "identity is invalid"):
                read_runtime_manifest(runtime)

    def test_manifest_reader_rejects_unsafe_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            manifest = runtime / "runtime-manifest.json"
            manifest.write_text("{}")
            manifest.chmod(0o644)
            with self.assertRaisesRegex(AuthorityError, "unsafe"):
                read_runtime_manifest(runtime)

    def test_manifest_reader_rejects_hard_link_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            self._write_manifest(runtime)
            alias = runtime / "manifest-alias.json"
            os.link(runtime / "runtime-manifest.json", alias)
            with self.assertRaisesRegex(AuthorityError, "unsafe"):
                read_runtime_manifest(runtime)

    def test_manifest_verifier_reads_complete_content_across_short_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            manifest = runtime / "runtime-manifest.json"
            manifest.write_text('{"release":"v1.2.3","files":["runtime.py"]}\n')
            manifest.chmod(0o600)
            digest = sha256(manifest.read_bytes()).hexdigest()
            original_read = os.read

            def short_read(descriptor: int, size: int) -> bytes:
                return original_read(descriptor, min(size, 3))

            with patch("tools.runtime_bootstrap.os.read", side_effect=short_read):
                self.assertTrue(verify_runtime_manifest(runtime, digest))

    def test_manifest_verifier_rejects_replaced_file_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            manifest = runtime / "runtime-manifest.json"
            manifest.write_text('{"release":"v1.2.3"}\n')
            manifest.chmod(0o600)
            digest = sha256(manifest.read_bytes()).hexdigest()
            original = manifest.stat()
            replacement = runtime / "replacement.json"
            replacement.write_bytes(manifest.read_bytes())
            replacement.chmod(0o600)
            manifest.unlink()
            replacement.rename(manifest)
            with self.assertRaisesRegex(AuthorityError, "identity changed"):
                verify_runtime_manifest(
                    runtime,
                    digest,
                    expected_file_identity=(original.st_dev, original.st_ino),
                )

    def test_manifest_verifier_rejects_parent_replacement_after_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            runtime.mkdir(mode=0o700)
            manifest = runtime / "runtime-manifest.json"
            manifest.write_text('{"release":"v1.2.3"}\n')
            manifest.chmod(0o600)
            digest = sha256(manifest.read_bytes()).hexdigest()
            original_lstat = Path.lstat
            parent_lstat_calls = 0

            def replace_parent(path: Path) -> os.stat_result:
                nonlocal parent_lstat_calls
                if path == runtime:
                    parent_lstat_calls += 1
                    if parent_lstat_calls == 2:
                        moved = runtime.with_name("runtime-original")
                        runtime.rename(moved)
                        runtime.symlink_to(moved, target_is_directory=True)
                return original_lstat(path)

            with (
                patch(
                    "tools.runtime_bootstrap.Path.lstat",
                    autospec=True,
                    side_effect=replace_parent,
                ),
                self.assertRaisesRegex(AuthorityError, "parent identity changed"),
            ):
                verify_runtime_manifest(runtime, digest)

    def test_rejects_symlinked_release_root_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real"
            real.mkdir(mode=0o700)
            (real / "v1.2.3").mkdir(mode=0o700)
            selector = root / "runtime-selector.json"
            commit_runtime_selector(selector, "v1.2.3", "v1.2.2")
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(AuthorityError, "contains a symlink"):
                resolve_selected_runtime(
                    selector, alias, self._identity_for_release(), self._verifier
                )

    @staticmethod
    def _identity_for_release() -> ExpectedRuntimeIdentity:
        return ExpectedRuntimeIdentity(
            "a" * 40, "refs/tags/v1.2.3", "b" * 40, "c" * 64, "d" * 64, "e" * 64
        )

    @staticmethod
    def _identity(_runtime: Path) -> ExpectedRuntimeIdentity:
        return RuntimeBootstrapTests._identity_for_release()

    @staticmethod
    def _verifier(runtime: Path, expected: ExpectedRuntimeIdentity) -> VerifiedManifest:
        digest = sha256(runtime.joinpath("runtime-manifest.json").read_bytes()).hexdigest()
        return VerifiedManifest("v1.2.3", expected, digest)
