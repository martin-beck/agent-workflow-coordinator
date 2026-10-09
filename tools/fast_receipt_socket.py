# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
"""Private opt-in receipt socket; a reply never upgrades a queued intent to authority."""

from __future__ import annotations

import json
import os
import socket
import stat
import struct
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

_SOCKET_NAME = "fast-receipts.sock"
_MAX_REQUEST = 4096
_MAX_REPLY = 16384
_PUBLIC_FIELDS = (
    "receipt_id",
    "project_id",
    "operation",
    "task_id",
    "expected_revision",
    "phase",
    "started_at",
    "commit_oid",
    "result_revision",
    "error_code",
    "remote_oid",
    "remote_observed_at",
    "publication_error",
    "created_at",
)


def public_receipt(row: dict[str, Any]) -> dict[str, Any]:
    """Expose the exact bounded public receipt without private input or key."""
    return {field: row[field] for field in _PUBLIC_FIELDS}


def _client_common_dir(root: Path) -> Path | None:
    """Resolve ordinary main/worktree Git metadata without launching Git."""
    marker = root / ".git"
    if marker.is_symlink():
        raise RuntimeError("fast socket Git metadata is a symlink")
    if marker.is_dir():
        return marker.resolve()
    if not marker.is_file():
        return None
    value = marker.read_text(encoding="utf-8")
    if len(value) > 1024 or not value.startswith("gitdir: "):
        return None
    name = value.removeprefix("gitdir: ").strip()
    gitdir = Path(name)
    if not gitdir.is_absolute():
        gitdir = root / gitdir
    gitdir = gitdir.resolve()
    common = gitdir / "commondir"
    if common.exists():
        name = common.read_text(encoding="utf-8").strip()
        if not name or len(name) > 1024:
            return None
        common_path = Path(name)
        if not common_path.is_absolute():
            common_path = gitdir / common_path
        return common_path.resolve()
    return gitdir


def _socket_path(root: Path) -> Path | None:
    common = _client_common_dir(root)
    return None if common is None else common / "handoffctl" / _SOCKET_NAME


def _safe_socket(path: Path) -> bool:
    try:
        directory = path.parent.lstat()
        entry = path.lstat()
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISDIR(directory.st_mode)
        or directory.st_uid != os.geteuid()
        or stat.S_IMODE(directory.st_mode) & 0o077
        or not stat.S_ISSOCK(entry.st_mode)
        or entry.st_uid != os.geteuid()
        or entry.st_nlink != 1
        or stat.S_IMODE(entry.st_mode) != 0o600
    ):
        raise RuntimeError("fast receipt socket is unsafe")
    return True


def _fast_request(argv: list[str]) -> dict[str, Any] | None:
    if argv[:2] == ["fast", "receipt"] and len(argv) == 3:
        return {"protocol": 1, "action": "receipt", "receipt_id": argv[2]}
    if argv[:2] != ["fast", "heartbeat"] or len(argv) < 9:
        return None
    if argv[2].startswith("-") or len(argv[3:]) % 2:
        return None
    options: dict[str, str] = {}
    for name, value in zip(argv[3::2], argv[4::2], strict=True):
        if name not in {"--owner", "--expected-revision", "--lease-minutes", "--key"}:
            return None
        if name in options:
            return None
        options[name] = value
    if not {"--owner", "--expected-revision", "--key"} <= options.keys():
        return None
    try:
        revision = int(options["--expected-revision"])
        lease = int(options.get("--lease-minutes", "120"))
    except ValueError:
        return None
    return {
        "protocol": 1,
        "action": "heartbeat",
        "task": argv[2],
        "owner": options["--owner"],
        "expected_revision": revision,
        "lease_minutes": lease,
        "key": options["--key"],
    }


def _read_line(peer: socket.socket, limit: int) -> bytes:
    data = bytearray()
    while len(data) <= limit:
        chunk = peer.recv(min(4096, limit + 1 - len(data)))
        if not chunk:
            raise RuntimeError("fast receipt socket closed before response")
        data.extend(chunk)
        if b"\n" in chunk:
            line, remainder = bytes(data).split(b"\n", 1)
            if remainder or len(line) > limit:
                raise RuntimeError("fast receipt socket response is malformed")
            return line
    raise RuntimeError("fast receipt socket message is too large")


def try_socket_fast(argv: list[str]) -> int | None:
    """Use the warm bound service when available; otherwise use the direct CLI."""
    request = _fast_request(argv)
    if request is None:
        return None
    path = _socket_path(Path(__file__).resolve().parent.parent)
    if path is None or not _safe_socket(path):
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(10)
            peer.connect(str(path))
            peer.sendall(json.dumps(request, sort_keys=True).encode() + b"\n")
            raw = _read_line(peer, _MAX_REPLY)
    except (ConnectionError, OSError, TimeoutError):
        # A timed-out enqueue may have committed; the direct path uses the same
        # idempotency key and returns that durable row rather than duplicating it.
        return None
    response = json.loads(raw)
    if not isinstance(response, dict) or set(response) not in ({"ok"}, {"error"}):
        raise RuntimeError("fast receipt socket response is invalid")
    if "error" in response:
        sys.stderr.write(f"ERROR: {response['error']}\n")
        return 1
    print(json.dumps(response["ok"], sort_keys=True))
    return 0


def _require_request(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict) or request.get("protocol") != 1:
        raise RuntimeError("invalid fast receipt socket protocol")
    action = request.get("action")
    if action == "receipt":
        if set(request) != {"protocol", "action", "receipt_id"}:
            raise RuntimeError("invalid fast receipt lookup")
        return request
    if action == "heartbeat":
        if set(request) != {
            "protocol",
            "action",
            "task",
            "owner",
            "expected_revision",
            "lease_minutes",
            "key",
        }:
            raise RuntimeError("invalid fast heartbeat request")
        if (
            not isinstance(request["expected_revision"], int)
            or isinstance(request["expected_revision"], bool)
            or not isinstance(request["lease_minutes"], int)
            or isinstance(request["lease_minutes"], bool)
            or not all(isinstance(request[field], str) for field in ("task", "owner", "key"))
        ):
            raise RuntimeError("invalid fast heartbeat request fields")
        return request
    raise RuntimeError("unknown fast receipt socket action")


def _handle(core: Any, peer: socket.socket, path: Path) -> None:
    from .fast_receipts import ReceiptStore

    with peer:
        try:
            peer.settimeout(10)
            credential = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            pid, uid, _gid = struct.unpack("3i", credential)
            if uid != os.geteuid() or pid < 1:
                raise RuntimeError("fast receipt socket peer is not authorized")
            caller = Path(f"/proc/{pid}/cwd").readlink().resolve()
            core.assert_project_binding(caller)
            if core.backend_selection()["backend"] != "git":
                raise RuntimeError("fast receipts require Git authority")
            request = _require_request(json.loads(_read_line(peer, _MAX_REQUEST)))
            with ReceiptStore(
                path.parent / "fast-receipts.sqlite3",
                core.project_binding()["project_id"],
            ) as store:
                if request["action"] == "heartbeat":
                    result = store.enqueue_heartbeat(
                        key=request["key"],
                        task=request["task"],
                        owner=request["owner"],
                        expected_revision=request["expected_revision"],
                        lease_minutes=request["lease_minutes"],
                    )
                else:
                    found = store.read(request["receipt_id"])
                    if found is None:
                        raise RuntimeError("unknown fast receipt")
                    result = found
            reply = {"ok": public_receipt(result)}
            peer.sendall(json.dumps(reply, sort_keys=True).encode() + b"\n")
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            with suppress(OSError):
                peer.sendall(json.dumps({"error": str(error)}).encode() + b"\n")


@contextmanager
def socket_service(core: Any) -> Iterator[None]:  # noqa: C901
    """Serve queue/read requests beside, not inside, the authority executor."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    path = core.coordinator_lock_path().parent / _SOCKET_NAME
    if path.exists() or path.is_symlink():
        if not _safe_socket(path):
            raise RuntimeError("fast receipt socket path is unsafe")
        old = path.lstat()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.1)
            try:
                probe.connect(str(path))
            except ConnectionRefusedError:
                pass
            else:
                raise RuntimeError("fast receipt socket is already active")
        if path.lstat().st_ino != old.st_ino:
            raise RuntimeError("fast receipt socket identity changed")
        path.unlink()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        path.chmod(0o600)
        if not _safe_socket(path):
            raise RuntimeError("fast receipt socket did not bind privately")
        identity = path.lstat().st_ino
        listener.listen(64)
        listener.settimeout(0.05)
        stop = threading.Event()
        slots = threading.BoundedSemaphore(64)
        with ThreadPoolExecutor(max_workers=32) as pool:

            def accept_loop() -> None:
                while not stop.is_set():
                    try:
                        peer, _ = listener.accept()
                    except TimeoutError:
                        continue
                    except OSError:
                        if stop.is_set():
                            break
                        raise
                    if not slots.acquire(blocking=False):
                        peer.close()
                        continue

                    def handle_one(connection: socket.socket) -> None:
                        try:
                            _handle(core, connection, path)
                        finally:
                            slots.release()

                    pool.submit(handle_one, peer)

            thread = threading.Thread(target=accept_loop, daemon=True)
            thread.start()
            try:
                yield
            finally:
                stop.set()
                thread.join(timeout=2)
        if path.exists() and path.lstat().st_ino == identity:
            path.unlink()
