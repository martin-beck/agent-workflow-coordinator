# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Opt-in Git receipt commands; queue admission is not authority completion."""

from __future__ import annotations

import json
from argparse import Namespace
from typing import Any

from .fast_receipt_worker import process_pending, publish_pending
from .fast_receipts import ReceiptStore

_PUBLIC_FIELDS = (
    "receipt_id",
    "project_id",
    "operation",
    "task_id",
    "expected_revision",
    "phase",
    "commit_oid",
    "result_revision",
    "error_code",
    "remote_oid",
    "remote_observed_at",
    "publication_error",
    "created_at",
)


def public_receipt(row: dict[str, Any]) -> dict[str, Any]:
    """Return evidence fields without internal payload or idempotency key."""
    return {field: row[field] for field in _PUBLIC_FIELDS}


def dispatch_fast(core: Any, args: Namespace) -> int:
    """Access one bound per-project queue without changing strict CLI semantics."""
    core.assert_project_binding()
    if core.backend_selection()["backend"] != "git":
        raise RuntimeError("fast receipts require Git authority")
    path = core.coordinator_lock_path().parent / "fast-receipts.sqlite3"
    with ReceiptStore(path, core.project_binding()["project_id"]) as store:
        if args.fast_action == "heartbeat":
            receipt = store.enqueue_heartbeat(
                key=args.key,
                task=args.task,
                owner=args.owner,
                expected_revision=args.expected_revision,
                lease_minutes=args.lease_minutes,
            )
            print(json.dumps(public_receipt(receipt), sort_keys=True))
            return 0
        if args.fast_action == "receipt":
            found = store.read(args.receipt_id)
            if found is None:
                raise RuntimeError("unknown fast receipt")
            print(json.dumps(public_receipt(found), sort_keys=True))
            return 0
        if args.fast_action == "worker":
            local = process_pending(core, store, limit=args.limit)
            remote = publish_pending(core, store)
            print(
                json.dumps(
                    {
                        "local": [public_receipt(item) for item in local],
                        "remote": [public_receipt(item) for item in remote],
                    },
                    sort_keys=True,
                )
            )
            return 0
    raise RuntimeError("unknown fast receipt action")
