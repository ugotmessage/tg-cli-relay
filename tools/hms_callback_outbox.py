#!/usr/bin/env python3
"""Standalone HMS → WEB1 callback outbox (stdlib only).

Enqueue signs a terminal event and writes pending/<event_id>.json BEFORE any
delivery. Drain sends pending records over argv-array ssh (stdin = event bytes).

This module must not import tg_cli_relay. HMAC canonicalization is duplicated
on purpose and is proven identical by tests.

Watchdog integration (after deploy copies this file to
``/Users/ht/.hermes/scripts/research/hms_callback_outbox.py``):

* Call ``enqueue`` only after the queue file is already in ``done/`` or
  ``failed/`` (after the existing replace + notify/log).
* Wrap enqueue/drain in try/except so transport failure never replace()s
  queue files, never changes notify text, and never raises into the
  classifier.
* Map: success→complete; WorkerDoneValidationError→worker_done_validation_failed;
  TaskContractError→blocked_before_worker; Exception→failed.
* One bounded ``drain`` from the existing daemon loop: before idle sleep(10)
  AND after each task before sleep(2).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

MAX_EVENT_BYTES = 64 * 1024
MAX_SNAPSHOT_BYTES = 8192
MAX_CONTROLLER_DETAIL = 2048
MAX_HANDOFF_FILE_BYTES = 1 * 1024 * 1024
MAX_DRAIN_ATTEMPTS = 5
SSH_TIMEOUT_SECONDS = 30
DEFAULT_ALLOWED_ROOTS = (
    Path("/Users/ht/.hermes/artifacts"),
    Path("/private/tmp"),
    Path("/Volumes/home"),
)
ALLOWED_CONTROLLER_RESULTS = frozenset(
    {
        "complete",
        "worker_done_validation_failed",
        "blocked_before_worker",
        "failed",
    }
)

# Tests may replace this hook. Production default is subprocess.run argv ssh.
SshRunner = Callable[[list[str], bytes, float], subprocess.CompletedProcess]
ssh_runner: SshRunner | None = None


def canonical_event_body(event: Mapping[str, Any]) -> bytes:
    payload = {k: v for k, v in event.items() if k != "hmac"}
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def sign_event(event: Mapping[str, Any], secret: bytes) -> str:
    return hmac.new(secret, canonical_event_body(event), hashlib.sha256).hexdigest()


def load_secret_file(path: Path) -> bytes:
    data = path.read_bytes()
    if not data:
        raise SystemExit("empty_secret")
    stripped = data.strip()
    if not stripped:
        raise SystemExit("empty_secret")
    if len(stripped) == 64 and all(c in b"0123456789abcdefABCDEF" for c in stripped):
        return bytes.fromhex(stripped.decode("ascii"))
    if len(data) == 32:
        return data
    if len(stripped) == 32:
        return stripped
    raise SystemExit("invalid_secret")


def allowed_artifact_roots() -> tuple[Path, ...]:
    raw = os.environ.get("HMS_CALLBACK_ALLOWED_ROOTS", "").strip()
    if raw:
        return tuple(Path(item.strip()) for item in raw.split(",") if item.strip())
    return DEFAULT_ALLOWED_ROOTS


def _is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink() or os.path.islink(path)
    except OSError:
        return False


def _under(path: Path, roots: tuple[Path, ...]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def validate_existing_path(path_str: str, *, must_exist: bool) -> Path:
    if not isinstance(path_str, str) or not path_str.startswith("/") or "\x00" in path_str:
        raise SystemExit("malformed")
    path = Path(path_str)
    if ".." in path.parts:
        raise SystemExit("traversal")
    if _is_symlink(path):
        raise SystemExit("symlink")
    if not _under(path, allowed_artifact_roots()):
        raise SystemExit("disallowed")
    if must_exist and not path.is_file():
        raise SystemExit("missing_handoff")
    if path.exists() and _is_symlink(path):
        raise SystemExit("symlink")
    if path.exists() and path.is_file():
        size = path.stat().st_size
        if size > MAX_HANDOFF_FILE_BYTES:
            raise SystemExit("oversized")
    return path


def freeze_handoff(path: Path) -> tuple[str | None, int | None, str | None]:
    if not path.is_file():
        return None, None, None
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    snapshot = data.decode("utf-8", "replace") if len(data) <= MAX_SNAPSHOT_BYTES else None
    if snapshot is not None and len(snapshot.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        snapshot = None
    return digest, len(data), snapshot


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def outbox_root() -> Path:
    raw = os.environ.get("HMS_CALLBACK_OUTBOX", "").strip()
    if not raw:
        raise SystemExit("misconfigured")
    root = Path(raw)
    if _is_symlink(root):
        raise SystemExit("misconfigured")
    for state in ("pending", "sent", "failed"):
        (root / state).mkdir(parents=True, exist_ok=True)
    return root


def build_signed_event(
    *,
    task_id: str,
    controller_result: str,
    controller_detail: str,
    handoff_path: str,
    artifact: str | None,
    secret: bytes,
    event_id: str | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    if controller_result not in ALLOWED_CONTROLLER_RESULTS:
        raise SystemExit("malformed")
    handoff = validate_existing_path(handoff_path, must_exist=False)
    sha, size, snapshot = freeze_handoff(handoff) if (handoff.exists() and handoff.is_file()) else (None, None, None)
    artifact_path = None
    if artifact:
        artifact_path = str(validate_existing_path(artifact, must_exist=False))
    status = "done" if controller_result == "complete" else "failed"
    event: dict[str, Any] = {
        "schema_version": 1,
        "event_id": event_id or str(uuid.uuid4()),
        "task_id": str(task_id),
        "status": status,
        "controller_result": controller_result,
        "controller_detail": str(controller_detail)[:MAX_CONTROLLER_DETAIL],
        "occurred_at": occurred_at or utcnow_iso(),
        "handoff_path": str(handoff),
        "handoff_sha256": sha,
        "handoff_size": size,
        "handoff_snapshot": snapshot,
        "artifact": artifact_path,
    }
    event["hmac"] = sign_event(event, secret)
    return event


def encode_event(event: Mapping[str, Any]) -> bytes:
    body = (json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if len(body) > MAX_EVENT_BYTES:
        raise SystemExit("oversized")
    return body


def ssh_argv() -> list[str]:
    config = os.environ.get("HMS_CALLBACK_SSH_CONFIG", "").strip()
    alias = os.environ.get("HMS_CALLBACK_SSH_ALIAS", "").strip()
    remote_cmd = os.environ.get("HMS_CALLBACK_SSH_REMOTE_CMD", "").strip()
    if not config or not alias or not remote_cmd:
        raise SystemExit("misconfigured")
    return [
        "ssh",
        "-F",
        config,
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        alias,
        remote_cmd,
    ]


def run_ssh(argv: list[str], event_bytes: bytes, timeout: float) -> subprocess.CompletedProcess:
    runner = ssh_runner
    if runner is not None:
        return runner(argv, event_bytes, timeout)
    return subprocess.run(
        argv,
        input=event_bytes,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def exclusive_write(path: Path, data: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return False
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def write_json(path: Path, obj: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes((json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    os.replace(tmp, path)


def callback_opt_in(task: Mapping[str, Any]) -> dict[str, Any] | None:
    callback = task.get("callback")
    if not isinstance(callback, dict):
        return None
    enabled = callback.get("enabled")
    if enabled is True:
        return callback
    if isinstance(enabled, str) and enabled.strip().lower() in {"1", "true", "yes", "on"}:
        return callback
    return None


def cmd_enqueue(task_json: Path, controller_result: str, controller_detail: str) -> int:
    task = json.loads(task_json.read_text(encoding="utf-8"))
    if not isinstance(task, dict):
        print("malformed", file=sys.stderr)
        return 3
    callback = callback_opt_in(task)
    if callback is None:
        print("skipped")
        return 0
    task_id = task.get("id")
    if not isinstance(task_id, str) or not task_id:
        print("malformed", file=sys.stderr)
        return 3
    handoff_path = callback.get("handoff_path")
    if not isinstance(handoff_path, str):
        print("malformed", file=sys.stderr)
        return 3
    artifact = callback.get("artifact")
    if artifact is not None and not isinstance(artifact, str):
        print("malformed", file=sys.stderr)
        return 3
    secret_path = os.environ.get("HMS_CALLBACK_SECRET_FILE", "").strip()
    if not secret_path:
        print("misconfigured", file=sys.stderr)
        return 4
    secret = load_secret_file(Path(secret_path))
    event = build_signed_event(
        task_id=task_id,
        controller_result=controller_result,
        controller_detail=controller_detail,
        handoff_path=handoff_path,
        artifact=artifact,
        secret=secret,
    )
    root = outbox_root()
    event_id = str(event["event_id"])
    if (root / "sent" / f"{event_id}.json").exists():
        print("already_sent")
        return 0
    record = {
        "event": event,
        "attempts": 0,
        "next_ok_at": 0,
        "last_rc": None,
        "last_reason": None,
    }
    pending = root / "pending" / f"{event_id}.json"
    payload = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if not exclusive_write(pending, payload):
        print("pending_exists")
        return 0
    print("enqueued")
    return 0


def _reason_from_output(completed: subprocess.CompletedProcess) -> str:
    stdout = completed.stdout or b""
    if isinstance(stdout, bytes):
        text = stdout.decode("utf-8", "replace")
    else:
        text = str(stdout)
    return text.strip().splitlines()[0] if text.strip() else ""


def _durable_outcome(rc: int, reason: str) -> str | None:
    if rc == 0 or reason in {"accepted", "already_sent"}:
        return "sent"
    if rc == 2 or reason == "unknown_task":
        return "failed"
    if rc == 3 and reason == "replay":
        return "sent"
    return None


def cmd_drain() -> int:
    root = outbox_root()
    now = time.time()
    for pending in sorted((root / "pending").glob("*.json")):
        record = json.loads(pending.read_text(encoding="utf-8"))
        event = record.get("event") if isinstance(record, dict) else None
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("event_id") or pending.stem)
        sent_path = root / "sent" / f"{event_id}.json"
        if sent_path.exists():
            pending.unlink(missing_ok=True)
            continue
        attempts = int(record.get("attempts") or 0)
        next_ok_at = float(record.get("next_ok_at") or 0)
        if next_ok_at > now:
            continue
        event_bytes = encode_event(event)
        argv = ssh_argv()
        try:
            completed = run_ssh(argv, event_bytes, SSH_TIMEOUT_SECONDS)
        except Exception as exc:
            completed = subprocess.CompletedProcess(argv, 99, b"", str(exc).encode("utf-8"))
        reason = _reason_from_output(completed)
        rc = int(completed.returncode)
        record["attempts"] = attempts + 1
        record["last_rc"] = rc
        record["last_reason"] = reason
        outcome = _durable_outcome(rc, reason)
        if outcome == "sent":
            record["sent_at"] = utcnow_iso()
            write_json(sent_path, record)
            pending.unlink(missing_ok=True)
            continue
        if outcome == "failed" or record["attempts"] >= MAX_DRAIN_ATTEMPTS:
            record["failed_at"] = utcnow_iso()
            write_json(root / "failed" / f"{event_id}.json", record)
            pending.unlink(missing_ok=True)
            continue
        backoff = min(60, 2 ** record["attempts"])
        record["next_ok_at"] = now + backoff
        write_json(pending, record)
    print("drained")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hms_callback_outbox")
    sub = parser.add_subparsers(dest="cmd", required=True)
    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("--task-json", required=True, type=Path)
    enqueue.add_argument("--controller-result", required=True)
    enqueue.add_argument("--controller-detail", default="")
    sub.add_parser("drain")
    args = parser.parse_args(argv)
    if args.cmd == "enqueue":
        return cmd_enqueue(args.task_json, args.controller_result, args.controller_detail)
    return cmd_drain()


if __name__ == "__main__":
    raise SystemExit(main())
