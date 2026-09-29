# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Fresh-state campaign evidence bound to the coordinator's real release tags."""

from __future__ import annotations

import hashlib
import os
import subprocess
import unittest
from pathlib import Path
from typing import Any

from tests.test_upgrade_campaign import FAILURE_POINTS, _execute_generated_operations
from tools.generate_upgrade_contract import generate
from tools.verify_release_identity import verify_transition


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _bytes_at(root: Path, revision: str, path: str) -> bytes:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "show", f"{revision}:{path}"],  # noqa: S607
        check=True,
        capture_output=True,
    )
    return result.stdout


def _release_identity(root: Path, version: str) -> dict[str, str]:
    reference = f"refs/tags/{version}"
    tag_object = _git(root, "rev-parse", reference)
    source_commit = _git(root, "rev-parse", f"{reference}^{{commit}}")
    tag_type = _git(root, "cat-file", "-t", reference)
    # The release policy permits unsigned lightweight and unsigned annotated
    # tags. The verifier records the all-zero signature digest for both.
    if tag_type == "tag":
        tag_contents = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), "cat-file", "tag", tag_object],  # noqa: S607
            check=True,
            capture_output=True,
        ).stdout
        begin = tag_contents.find(b"-----BEGIN SSH SIGNATURE-----")
        end = tag_contents.find(b"-----END SSH SIGNATURE-----", begin)
        if begin >= 0 and end >= 0:
            end += len(b"-----END SSH SIGNATURE-----")
            signature = hashlib.sha256(tag_contents[begin:end]).hexdigest()
        else:
            signature = "0" * 64
    else:
        signature = "0" * 64
    return {
        "version": version,
        "source_commit": source_commit,
        "tag_ref": reference,
        "tag_object": tag_object,
        "signature_sha256": signature,
        "trust_policy_sha256": hashlib.sha256(
            _bytes_at(root, source_commit, "docs/QUALITY.md")
        ).hexdigest(),
        "vendor_manifest_sha256": hashlib.sha256(
            _bytes_at(root, source_commit, "uv.lock")
        ).hexdigest(),
    }


def _campaign_transition(root: Path) -> tuple[dict[str, Any], bool]:
    from_version = os.environ.get("AWC_CAMPAIGN_FROM_TAG", "v0.3.46")
    to_version = os.environ.get("AWC_CAMPAIGN_TO_TAG", "v0.3.47")
    old = _release_identity(root, from_version)
    target_exists = bool(_git(root, "tag", "--list", to_version))
    if target_exists:
        new = _release_identity(root, to_version)
    else:
        head = _git(root, "rev-parse", "HEAD")
        new = {
            "version": to_version,
            "source_commit": head,
            "tag_ref": f"refs/tags/{to_version}",
            "tag_object": head,
            "signature_sha256": "0" * 64,
            "trust_policy_sha256": hashlib.sha256(
                _bytes_at(root, head, "docs/QUALITY.md")
            ).hexdigest(),
            "vendor_manifest_sha256": hashlib.sha256(
                _bytes_at(root, head, "uv.lock")
            ).hexdigest(),
        }
    return (
        {
            "operation_id": f"fresh-state:{from_version}-to-{to_version}",
            "backend": "git",
            "selector_ref": ".runtime/runtime-selector.json",
            "expected_state_revision": 1,
            "barrier_id": "fresh-state-campaign-barrier",
            "fencing_token": "fresh-state-campaign-fence",
            "from": old,
            "to": new,
        },
        target_exists,
    )


class FreshStateReleaseCampaignTests(unittest.TestCase):
    def test_exact_release_identity_and_failure_matrix(self) -> None:
        root = Path(__file__).resolve().parents[1]
        transition, target_exists = _campaign_transition(root)
        verify_transition(root, transition, candidate=not target_exists)

        for backend in ("git", "sqlite"):
            with self.subTest(backend=backend):
                backend_transition = dict(transition, backend=backend)
                document = generate(backend_transition)
                for failure in FAILURE_POINTS:
                    with self.subTest(failure=failure):
                        records = _execute_generated_operations(document, failure)
                        if failure in {"stage", "commit", "validate", "reopen"}:
                            self.assertEqual(
                                f"{document['operation_id']}:rollback",
                                records[-1]["operation_id"],
                            )
                        elif failure is None:
                            self.assertEqual("completed", records[-1]["outcome"])


if __name__ == "__main__":
    unittest.main()
