"""WEB1 verified completion callback: marker, registration, HMAC receiver, inbox.

Opt-in via TGR_BACKGROUND_HANDOFF (unset/0/false/no/off = disabled). WEB1 never
polls the HMS queue and never runs Codex. Destination identity is frozen at
registration from Relay context only; later events cannot retarget Telegram.
Independent verification happens on HMS; WEB1 only accepts a signed
registration probe or a signed final-result and delivers the frozen text.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from tg_cli_relay.providers.base import RunResult

log = logging.getLogger(__name__)

KIND_PROBE = "registration_probe"
KIND_FINAL = "final_result"

MARKER_RE = re.compile(
    r"^TGR_BACKGROUND_HANDOFF:([A-Za-z0-9][A-Za-z0-9._-]{0,127}):(/[^\s]+)$"
)
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

MAX_EVENT_BYTES = 64 * 1024
MAX_SNAPSHOT_BYTES = 8192
MAX_HANDOFF_PATH_CHARS = 4096
MAX_CONTROLLER_DETAIL = 2048
MAX_CLAIM_ATTEMPTS = 2
DEFAULT_SKEW_SECONDS = 300
MAX_VERIFIER_STDOUT = 8192
PENDING_DELIVERY_STATUS = "verified_pending_delivery"

FORBIDDEN_DEST_KEYS = frozenset(
    {
        "chat_id",
        "user_id",
        "thread_key",
        "message_thread_id",
        "message_id",
        "bot_fingerprint",
        "telegram_chat_id",
        "telegram_user_id",
        "dest",
        "destination",
    }
)
ALLOWED_STATUS = frozenset({"done", "failed"})
ALLOWED_CONTROLLER_RESULTS = frozenset(
    {
        "complete",
        "worker_done_validation_failed",
        "blocked_before_worker",
        "failed",
    }
)
REQUIRED_PROBE_KEYS = (
    "schema_version",
    "kind",
    "event_id",
    "task_id",
    "occurred_at",
    "hmac",
)
REQUIRED_FINAL_KEYS = (
    "schema_version",
    "kind",
    "event_id",
    "task_id",
    "status",
    "controller_result",
    "controller_detail",
    "occurred_at",
    "handoff_path",
    "handoff_sha256",
    "handoff_size",
    "artifact",
    "verifier_stdout",
    "verifier_returncode",
    "hmac",
)
REQUIRED_EVENT_KEYS = REQUIRED_FINAL_KEYS

PENDING_ACK = "已登記背景工作，完成後會獨立驗收並回覆此對話。"
REGISTER_FAIL_ACK = "無法登記背景工作，請稍後再試。"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"", "0", "false", "no", "off"})


class CallbackError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CallbackConfigError(CallbackError):
    pass


@dataclass(frozen=True)
class ReceiveResult:
    code: int
    reason: str


def feature_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    raw = source.get("TGR_BACKGROUND_HANDOFF")
    if raw is None:
        return False
    return raw.strip().lower() in _TRUTHY


def pending_ack_text(_task_id: str | None = None) -> str:
    return PENDING_ACK


def register_fail_ack_text() -> str:
    return REGISTER_FAIL_ACK


def canonical_event_body(event: Mapping[str, Any]) -> bytes:
    payload = {k: v for k, v in event.items() if k != "hmac"}
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def sign_event(event: Mapping[str, Any], secret: bytes) -> str:
    return hmac.new(secret, canonical_event_body(event), hashlib.sha256).hexdigest()


def verify_event_hmac(event: Mapping[str, Any], secret: bytes) -> bool:
    given = event.get("hmac")
    if not isinstance(given, str) or not given:
        return False
    expected = sign_event(event, secret)
    try:
        return hmac.compare_digest(given, expected)
    except (TypeError, ValueError):
        return False


def load_secret_file(path: Path) -> bytes:
    data = path.read_bytes()
    if not data:
        raise CallbackConfigError("empty_secret")
    stripped = data.strip()
    if not stripped:
        raise CallbackConfigError("empty_secret")
    if len(stripped) == 64 and all(c in b"0123456789abcdefABCDEF" for c in stripped):
        return bytes.fromhex(stripped.decode("ascii"))
    if len(data) == 32:
        return data
    if len(stripped) == 32:
        return stripped
    raise CallbackConfigError("invalid_secret")


def bot_fingerprint_from_env(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    token = source.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return "default"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def git_toplevel(start: Path | None = None) -> Path | None:
    cur = (start or Path.cwd()).resolve()
    for parent in (cur, *cur.parents):
        if (parent / ".git").exists():
            return parent
    return None


def _is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink() or os.path.islink(path)
    except OSError:
        return False


def resolve_state_root(env: Mapping[str, str] | None = None, *, create: bool = True) -> Path:
    source = os.environ if env is None else env
    raw = (source.get("TGR_BACKGROUND_HANDOFF_STATE_ROOT") or "").strip()
    if not raw:
        raise CallbackConfigError("misconfigured")
    path = Path(raw)
    if _is_symlink(path):
        raise CallbackConfigError("symlink_state_root")
    for parent in path.parents:
        if parent.exists() and _is_symlink(parent):
            raise CallbackConfigError("symlink_state_root")
    if path.exists():
        resolved = path.resolve()
        if _is_symlink(path):
            raise CallbackConfigError("symlink_state_root")
    else:
        resolved = path if path.is_absolute() else path.resolve()
    repo = git_toplevel()
    if repo is not None:
        try:
            resolved.resolve().relative_to(repo.resolve())
        except ValueError:
            pass
        else:
            raise CallbackConfigError("state_root_inside_repo")
    if create:
        resolved.mkdir(parents=True, exist_ok=True)
        if _is_symlink(resolved):
            raise CallbackConfigError("symlink_state_root")
        ensure_state_layout(resolved)
    return resolved


def ensure_state_layout(root: Path) -> None:
    for sub in (
        "registrations",
        "inbox/new",
        "inbox/claimed",
        "inbox/completed",
        "inbox/failed",
        "seen",
        "receipts",
    ):
        (root / sub).mkdir(parents=True, exist_ok=True)


def exclusive_create(path: Path, data: bytes, *, mode: int = 0o600) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(path, flags, mode)
    except FileExistsError:
        return False
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def parse_handoff_marker(text: str) -> tuple[str, str] | None:
    if not isinstance(text, str):
        return None
    match = MARKER_RE.fullmatch(text.strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def find_handoff_markers(res: RunResult) -> tuple[list[str] | None, list[tuple[str, str]]]:
    """Return (remaining_segments_or_None, markers).

    Markers must be an entire agent_message / stdout segment, not a substring.
    When markers are found, remaining is a list (possibly empty). When none are
    found, remaining is None so callers keep the original body/segments.
    """
    markers: list[tuple[str, str]] = []
    if res.stdout_segments:
        remaining: list[str] = []
        for segment in res.stdout_segments:
            parsed = parse_handoff_marker(segment)
            if parsed:
                markers.append(parsed)
            else:
                remaining.append(segment)
        if markers:
            return remaining, markers
        return None, []
    parsed = parse_handoff_marker(res.stdout or "")
    if parsed:
        return [], [parsed]
    return None, []


def validate_handoff_path_ref(path_str: str) -> str:
    if not isinstance(path_str, str) or not path_str.startswith("/") or "\x00" in path_str:
        raise CallbackError("malformed")
    if len(path_str) > MAX_HANDOFF_PATH_CHARS:
        raise CallbackError("oversized")
    parts = Path(path_str).parts
    if ".." in parts:
        raise CallbackError("traversal")
    existing = Path(path_str)
    if existing.exists() or _is_symlink(existing):
        if _is_symlink(existing):
            raise CallbackError("symlink")
    return path_str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def identity_from_msg(msg: Any, thread_key: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    source = os.environ if env is None else env
    from_user = getattr(msg, "from_user", None)
    user_id = getattr(from_user, "id", None) if from_user is not None else None
    if user_id is None:
        user_id = getattr(msg, "user_id", None)
    if user_id is None:
        user_id = int(msg.chat_id)
    thread_id = getattr(msg, "message_thread_id", None)
    workspace = source.get("TGR_DEFAULT_WORKSPACE", "").strip()
    if not workspace:
        raise CallbackError("misconfigured")
    return {
        "chat_id": int(msg.chat_id),
        "message_thread_id": int(thread_id) if thread_id is not None else None,
        "thread_key": str(thread_key),
        "user_id": int(user_id),
        "bot_fingerprint": bot_fingerprint_from_env(source),
        "relay_workspace": workspace,
    }


def _registration_path(root: Path, bot_fp: str, task_id: str) -> Path:
    if not TASK_ID_RE.fullmatch(task_id) or "/" in bot_fp or ".." in bot_fp:
        raise CallbackError("malformed")
    return root / "registrations" / bot_fp / f"{task_id}.json"


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def register_from_relay_context(
    *,
    task_id: str,
    handoff_path: str,
    msg: Any,
    thread_key: str,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if not feature_enabled(env):
        raise CallbackConfigError("disabled")
    if not TASK_ID_RE.fullmatch(task_id):
        raise CallbackError("malformed")
    handoff_path = validate_handoff_path_ref(handoff_path)
    identity = identity_from_msg(msg, thread_key, env)
    record = {
        "task_id": task_id,
        "chat_id": identity["chat_id"],
        "message_thread_id": identity["message_thread_id"],
        "thread_key": identity["thread_key"],
        "relay_workspace": identity["relay_workspace"],
        "bot_fingerprint": identity["bot_fingerprint"],
        "user_id": identity["user_id"],
        "handoff_path": handoff_path,
        "created_at": _utcnow_iso(),
        "status": "registered",
    }
    root = resolve_state_root(env)
    path = _registration_path(root, record["bot_fingerprint"], task_id)
    payload = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if exclusive_create(path, payload):
        return record
    existing = load_json(path)
    if existing is None:
        raise CallbackError("conflict")
    frozen = (
        "task_id",
        "chat_id",
        "message_thread_id",
        "thread_key",
        "relay_workspace",
        "bot_fingerprint",
        "user_id",
        "handoff_path",
    )
    for key in frozen:
        if existing.get(key) != record.get(key):
            raise CallbackError("conflict")
    return existing


def find_registration(root: Path, task_id: str) -> dict[str, Any] | None:
    if not TASK_ID_RE.fullmatch(task_id):
        return None
    matches = sorted((root / "registrations").glob(f"*/{task_id}.json"))
    for path in matches:
        data = load_json(path)
        if data and data.get("task_id") == task_id:
            return data
    return None


def _secret_file_path(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    raw = (source.get("TGR_BACKGROUND_HANDOFF_SECRET_FILE") or "").strip()
    if not raw:
        raise CallbackConfigError("misconfigured")
    path = Path(raw)
    if _is_symlink(path):
        raise CallbackConfigError("misconfigured")
    return path


def _max_skew_seconds(env: Mapping[str, str] | None = None) -> int:
    source = os.environ if env is None else env
    raw = (source.get("TGR_BACKGROUND_HANDOFF_MAX_SKEW_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_SKEW_SECONDS
    try:
        value = int(raw)
    except ValueError as exc:
        raise CallbackConfigError("misconfigured") from exc
    if value < 0:
        raise CallbackConfigError("misconfigured")
    return value


def parse_occurred_at(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CallbackError("malformed")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CallbackError("malformed") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _looks_like_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def _common_receive_preamble(
    data: bytes, env: Mapping[str, str] | None
) -> tuple[ReceiveResult | None, dict[str, Any] | None, Path | None, int]:
    if not feature_enabled(env):
        return ReceiveResult(4, "disabled"), None, None, 0
    try:
        secret_path = _secret_file_path(env)
        secret = load_secret_file(secret_path)
        root = resolve_state_root(env)
        skew = _max_skew_seconds(env)
    except CallbackConfigError as exc:
        reason = exc.reason if exc.reason in {"disabled", "misconfigured"} else "misconfigured"
        return ReceiveResult(4, reason), None, None, 0
    except OSError:
        return ReceiveResult(4, "misconfigured"), None, None, 0

    if len(data) > MAX_EVENT_BYTES:
        return ReceiveResult(3, "oversized"), None, None, 0
    if not data.strip():
        return ReceiveResult(3, "malformed"), None, None, 0
    try:
        event = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ReceiveResult(3, "malformed"), None, None, 0
    if not isinstance(event, dict):
        return ReceiveResult(3, "malformed"), None, None, 0
    if FORBIDDEN_DEST_KEYS.intersection(event):
        return ReceiveResult(3, "poisoned"), None, None, 0
    if event.get("schema_version") != 1:
        return ReceiveResult(3, "malformed"), None, None, 0
    snapshot = event.get("handoff_snapshot")
    if snapshot is not None and (
        not isinstance(snapshot, str) or len(snapshot.encode("utf-8")) > MAX_SNAPSHOT_BYTES
    ):
        return ReceiveResult(3, "oversized"), None, None, 0
    if not isinstance(event.get("hmac"), str):
        return ReceiveResult(3, "bad_signature"), None, None, 0
    if not verify_event_hmac(event, secret):
        return ReceiveResult(3, "bad_signature"), None, None, 0
    return None, event, root, skew


def _validate_common_identity(event: Mapping[str, Any], skew: int) -> ReceiveResult | None:
    task_id = event.get("task_id")
    event_id = event.get("event_id")
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        return ReceiveResult(3, "malformed")
    if not isinstance(event_id, str) or not _looks_like_uuid(event_id):
        return ReceiveResult(3, "malformed")
    try:
        occurred = parse_occurred_at(str(event.get("occurred_at", "")))
    except CallbackError as exc:
        return ReceiveResult(3, exc.reason)
    now = _utcnow()
    delta = (now - occurred).total_seconds()
    if delta > skew:
        return ReceiveResult(3, "stale_timestamp")
    if -delta > skew:
        return ReceiveResult(3, "future_timestamp")
    return None


def _receive_probe(event: dict[str, Any], root: Path, skew: int) -> ReceiveResult:
    missing = [key for key in REQUIRED_PROBE_KEYS if key not in event]
    if missing:
        return ReceiveResult(3, "malformed")
    identity = _validate_common_identity(event, skew)
    if identity is not None:
        return identity
    task_id = str(event["task_id"])
    registration = find_registration(root, task_id)
    if registration is None:
        return ReceiveResult(2, "unknown_task")
    if registration.get("task_id") != task_id:
        return ReceiveResult(3, "task_id_mismatch")
    return ReceiveResult(0, "registered")


def _validate_optional_path(value: Any) -> ReceiveResult | None:
    if value is None:
        return None
    try:
        if not isinstance(value, str):
            raise CallbackError("malformed")
        validate_handoff_path_ref(value)
    except CallbackError as exc:
        return ReceiveResult(3, exc.reason)
    return None


def _receive_final(
    event: dict[str, Any], root: Path, skew: int, data: bytes
) -> ReceiveResult:
    missing = [key for key in REQUIRED_FINAL_KEYS if key not in event]
    if missing:
        return ReceiveResult(3, "malformed")
    identity = _validate_common_identity(event, skew)
    if identity is not None:
        return identity
    snapshot = event.get("handoff_snapshot")
    if snapshot is not None and (
        not isinstance(snapshot, str) or len(snapshot.encode("utf-8")) > MAX_SNAPSHOT_BYTES
    ):
        return ReceiveResult(3, "oversized")
    stdout = event.get("verifier_stdout")
    if not isinstance(stdout, str):
        return ReceiveResult(3, "malformed")
    if len(stdout.encode("utf-8")) > MAX_VERIFIER_STDOUT:
        return ReceiveResult(3, "oversized")
    stderr = event.get("verifier_stderr")
    if stderr is not None:
        if not isinstance(stderr, str) or len(stderr.encode("utf-8")) > MAX_CONTROLLER_DETAIL:
            return ReceiveResult(3, "malformed")
    rc = event.get("verifier_returncode")
    if not isinstance(rc, int) or isinstance(rc, bool):
        return ReceiveResult(3, "malformed")

    task_id = str(event["task_id"])
    event_id = str(event["event_id"])
    status = event.get("status")
    if status not in ALLOWED_STATUS:
        return ReceiveResult(3, "bad_status")
    if event.get("controller_result") not in ALLOWED_CONTROLLER_RESULTS:
        return ReceiveResult(3, "malformed")
    detail = event.get("controller_detail")
    if not isinstance(detail, str) or len(detail) > MAX_CONTROLLER_DETAIL:
        return ReceiveResult(3, "malformed")
    handoff_path = event.get("handoff_path")
    try:
        if not isinstance(handoff_path, str):
            raise CallbackError("malformed")
        validate_handoff_path_ref(handoff_path)
    except CallbackError as exc:
        return ReceiveResult(3, exc.reason)
    sha = event.get("handoff_sha256")
    if sha is not None and not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha)):
        return ReceiveResult(3, "malformed")
    size = event.get("handoff_size")
    if size is not None and not (isinstance(size, int) and not isinstance(size, bool) and size >= 0):
        return ReceiveResult(3, "malformed")
    for key in ("artifact", "report_path", "validation_path"):
        path_error = _validate_optional_path(event.get(key))
        if path_error is not None:
            return path_error

    registration = find_registration(root, task_id)
    if registration is None:
        return ReceiveResult(2, "unknown_task")
    if registration.get("task_id") != task_id:
        return ReceiveResult(3, "task_id_mismatch")

    seen_path = root / "seen" / event_id
    inbox_new = root / "inbox" / "new" / f"{event_id}.json"
    if seen_path.exists() or inbox_new.exists():
        existing = load_json(inbox_new) or {}
        if existing and existing.get("hmac") != event.get("hmac"):
            return ReceiveResult(3, "conflict")
        return ReceiveResult(3, "replay")

    existing_receipt = root / "receipts" / f"{task_id}.json"
    if existing_receipt.exists():
        return ReceiveResult(3, "conflict")
    for folder in ("claimed", "completed", "failed", "new"):
        for existing_path in (root / "inbox" / folder).glob("*.json"):
            existing = load_json(existing_path)
            payload = existing.get("event", existing) if existing else None
            if not payload:
                continue
            if payload.get("task_id") == task_id and payload.get("event_id") != event_id:
                return ReceiveResult(3, "conflict")

    payload = data if data.endswith(b"\n") else data + b"\n"
    if not exclusive_create(inbox_new, payload):
        return ReceiveResult(3, "replay")
    exclusive_create(seen_path, b"1\n")
    return ReceiveResult(0, "accepted")


def receive_event_bytes(data: bytes, env: Mapping[str, str] | None = None) -> ReceiveResult:
    early, event, root, skew = _common_receive_preamble(data, env)
    if early is not None:
        return early
    assert event is not None and root is not None
    kind = event.get("kind")
    if kind == KIND_PROBE:
        return _receive_probe(event, root, skew)
    if kind != KIND_FINAL:
        return ReceiveResult(3, "malformed")
    return _receive_final(event, root, skew, data)


def receive_from_stdin(stdin_buffer: Any | None = None, stdout: Any | None = None) -> int:
    import sys

    raw_in = stdin_buffer if stdin_buffer is not None else sys.stdin.buffer
    raw_out = stdout if stdout is not None else sys.stdout
    data = raw_in.read(MAX_EVENT_BYTES + 1)
    result = receive_event_bytes(data)
    raw_out.write(result.reason + "\n")
    if hasattr(raw_out, "flush"):
        raw_out.flush()
    return result.code


def _event_from_claimed(doc: dict[str, Any]) -> dict[str, Any]:
    if isinstance(doc.get("event"), dict):
        return doc["event"]
    return doc


def _claimed_attempts(doc: dict[str, Any]) -> int:
    try:
        return int(doc.get("claim_attempts") or 0)
    except (TypeError, ValueError):
        return 0


def receipt_path(root: Path, task_id: str) -> Path:
    return root / "receipts" / f"{task_id}.json"


def write_receipt(root: Path, record: dict[str, Any]) -> None:
    task_id = str(record["task_id"])
    path = receipt_path(root, task_id)
    payload = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if path.exists():
        path.write_bytes(payload)
        return
    exclusive_create(path, payload)


def _move_claimed(root: Path, event_id: str, dest_folder: str, doc: dict[str, Any]) -> None:
    dest = root / "inbox" / dest_folder / f"{event_id}.json"
    payload = (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    exclusive_create(dest, payload)
    claimed = root / "inbox" / "claimed" / f"{event_id}.json"
    if claimed.exists():
        claimed.unlink()


def frozen_delivery_text(event: Mapping[str, Any]) -> str:
    text = _bound_verifier_stdout(str(event.get("verifier_stdout") or "")).strip()
    if text:
        return text
    rc = event.get("verifier_returncode")
    if isinstance(rc, int) and not isinstance(rc, bool) and rc != 0:
        return format_verifier_failure(str(event.get("verifier_stderr") or f"returncode {rc}"))
    return "(無輸出)"


def format_failed_notice(event: Mapping[str, Any]) -> str:
    result = str(event.get("controller_result") or "failed")
    detail = str(event.get("controller_detail") or "").strip()[:400]
    if detail:
        return f"背景工作未通過（{result}）。獨立驗收未啟動。{detail}"
    return f"背景工作未通過（{result}）。獨立驗收未啟動。"


def format_interrupted_notice(event: Mapping[str, Any]) -> str:
    task_id = str(event.get("task_id") or "")
    return f"背景回呼處理中斷（{task_id}），已停止自動重試。"


def format_verifier_failure(detail: str) -> str:
    clipped = (detail or "verifier failed").strip()[:400]
    return f"背景工作驗收失敗：{clipped}"


def format_delivery_failure() -> str:
    return "背景工作驗收結果已產生，但 Telegram 傳送失敗。"


def _claim_new_event(root: Path, new_path: Path) -> dict[str, Any] | None:
    event = load_json(new_path)
    if event is None:
        return None
    event_id = str(event.get("event_id") or new_path.stem)
    claimed_path = root / "inbox" / "claimed" / f"{event_id}.json"
    doc = {"event": event, "claim_attempts": 0, "claimed_at": _utcnow_iso()}
    payload = (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if not exclusive_create(claimed_path, payload):
        return None
    try:
        new_path.unlink()
    except OSError:
        pass
    return doc


def recover_claimed_without_receipt(root: Path) -> None:
    claimed_dir = root / "inbox" / "claimed"
    if not claimed_dir.exists():
        return
    for path in sorted(claimed_dir.glob("*.json")):
        doc = load_json(path)
        if doc is None:
            continue
        event = _event_from_claimed(doc)
        task_id = str(event.get("task_id") or "")
        event_id = str(event.get("event_id") or path.stem)
        existing_receipt = load_json(receipt_path(root, task_id)) if task_id else None
        completed = root / "inbox" / "completed" / f"{event_id}.json"
        failed = root / "inbox" / "failed" / f"{event_id}.json"
        if existing_receipt is not None:
            receipt_status = existing_receipt.get("status")
            if receipt_status == PENDING_DELIVERY_STATUS:
                continue
            folder = "completed" if receipt_status == "completed" else "failed"
            if not completed.exists() and not failed.exists():
                _move_claimed(root, event_id, folder, doc)
            elif path.exists():
                path.unlink()
            continue
        if completed.exists() or failed.exists():
            if path.exists():
                path.unlink()
            continue


DeliverFn = Callable[[dict[str, Any], str], Awaitable[None]]
VerifierFn = Callable[..., RunResult | Awaitable[RunResult]]


async def scan_once(
    *,
    active_thread_keys: set[str],
    deliver: DeliverFn,
    verifier: VerifierFn | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    del verifier  # WEB1 never runs Codex; HMS already persisted the verdict.
    if not feature_enabled(env):
        return
    try:
        root = resolve_state_root(env)
    except CallbackConfigError:
        log.warning("callback inbox skipped: misconfigured state root")
        return
    recover_claimed_without_receipt(root)
    processed: set[str] = set()
    new_dir = root / "inbox" / "new"
    for new_path in sorted(new_dir.glob("*.json")):
        event = load_json(new_path)
        if event is None:
            continue
        registration = find_registration(root, str(event.get("task_id") or ""))
        if registration is None:
            event_id = str(event.get("event_id") or new_path.stem)
            fail_doc = {"event": event, "note": "unknown_task"}
            _move_claimed_from_new(root, new_path, event_id, fail_doc, "failed")
            continue
        thread_key = str(registration.get("thread_key") or "")
        if thread_key and thread_key in active_thread_keys:
            continue
        claimed = _claim_new_event(root, new_path)
        if claimed is None:
            continue
        event_id = str(_event_from_claimed(claimed).get("event_id") or new_path.stem)
        processed.add(event_id)
        await _process_claimed(root, claimed, registration, deliver=deliver)
    for claimed_path in sorted((root / "inbox" / "claimed").glob("*.json")):
        doc = load_json(claimed_path)
        if doc is None:
            continue
        event = _event_from_claimed(doc)
        event_id = str(event.get("event_id") or claimed_path.stem)
        if event_id in processed:
            continue
        task_id = str(event.get("task_id") or "")
        existing_receipt = load_json(receipt_path(root, task_id))
        if existing_receipt and existing_receipt.get("status") != PENDING_DELIVERY_STATUS:
            continue
        registration = find_registration(root, task_id)
        if registration is None:
            continue
        thread_key = str(registration.get("thread_key") or "")
        if thread_key and thread_key in active_thread_keys:
            continue
        await _process_claimed(root, doc, registration, deliver=deliver)


def _move_claimed_from_new(
    root: Path, new_path: Path, event_id: str, doc: dict[str, Any], folder: str
) -> None:
    dest = root / "inbox" / folder / f"{event_id}.json"
    payload = (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    exclusive_create(dest, payload)
    try:
        new_path.unlink()
    except OSError:
        pass


async def _process_claimed(
    root: Path,
    doc: dict[str, Any],
    registration: dict[str, Any],
    *,
    deliver: DeliverFn,
) -> None:
    event = _event_from_claimed(doc)
    event_id = str(event.get("event_id") or "")
    task_id = str(event.get("task_id") or "")
    claimed_path = root / "inbox" / "claimed" / f"{event_id}.json"
    existing_receipt = load_json(receipt_path(root, task_id))
    if existing_receipt is not None:
        receipt_status = existing_receipt.get("status")
        if receipt_status == "completed":
            _move_claimed(root, event_id, "completed", doc)
            return
        if receipt_status == PENDING_DELIVERY_STATUS:
            await _complete_pending_delivery(
                root, doc, registration, existing_receipt, deliver=deliver
            )
            return
        _move_claimed(root, event_id, "failed", doc)
        return
    attempts = _claimed_attempts(doc)
    if attempts >= MAX_CLAIM_ATTEMPTS:
        receipt = _terminal_receipt(event, registration, status="failed", note="interrupted")
        write_receipt(root, receipt)
        try:
            await deliver(registration, format_interrupted_notice(event))
        except Exception:
            log.exception("callback interrupted notice delivery failed task=%s", task_id)
        _move_claimed(root, event_id, "failed", doc)
        return
    doc["claim_attempts"] = attempts + 1
    claimed_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    status = event.get("status")
    if status not in ALLOWED_STATUS or event.get("kind") != KIND_FINAL:
        receipt = _terminal_receipt(event, registration, status="failed", note="bad_status")
        write_receipt(root, receipt)
        try:
            await deliver(registration, format_failed_notice(event))
        except Exception:
            log.exception("callback bad_status notice delivery failed task=%s", task_id)
        _move_claimed(root, event_id, "failed", doc)
        return

    text = frozen_delivery_text(event)
    rc = event.get("verifier_returncode")
    returncode = rc if isinstance(rc, int) and not isinstance(rc, bool) else None
    write_receipt(
        root,
        _pending_delivery_receipt(
            event,
            registration,
            verifier_stdout=text,
            verifier_returncode=returncode,
        ),
    )
    delivered = await _deliver_bounded(deliver, registration, text)
    if not delivered:
        return
    receipt = _terminal_receipt(
        event,
        registration,
        status="completed",
        note="verified",
        delivered=True,
        verifier_returncode=returncode,
        verifier_stdout=text,
    )
    write_receipt(root, receipt)
    _move_claimed(root, event_id, "completed", doc)


async def _deliver_bounded(
    deliver: DeliverFn, registration: dict[str, Any], text: str
) -> bool:
    try:
        await deliver(registration, text)
        return True
    except Exception:
        log.exception("callback telegram delivery failed")
        return False


def _bound_verifier_stdout(text: str) -> str:
    if not isinstance(text, str):
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_VERIFIER_STDOUT:
        return text
    return encoded[:MAX_VERIFIER_STDOUT].decode("utf-8", "replace")


def _pending_delivery_receipt(
    event: Mapping[str, Any],
    registration: Mapping[str, Any],
    *,
    verifier_stdout: str,
    verifier_returncode: int | None,
) -> dict[str, Any]:
    return {
        "task_id": event.get("task_id"),
        "event_id": event.get("event_id"),
        "status": PENDING_DELIVERY_STATUS,
        "controller_result": event.get("controller_result"),
        "note": PENDING_DELIVERY_STATUS,
        "delivered": False,
        "verifier_returncode": verifier_returncode,
        "verifier_stdout": _bound_verifier_stdout(verifier_stdout),
        "thread_key": registration.get("thread_key"),
        "created_at": _utcnow_iso(),
    }


async def _complete_pending_delivery(
    root: Path,
    doc: dict[str, Any],
    registration: dict[str, Any],
    pending: dict[str, Any],
    *,
    deliver: DeliverFn,
) -> None:
    event = _event_from_claimed(doc)
    event_id = str(event.get("event_id") or "")
    text = str(pending.get("verifier_stdout") or "").strip() or "(無輸出)"
    delivered = await _deliver_bounded(deliver, registration, text)
    rc = pending.get("verifier_returncode")
    returncode = rc if isinstance(rc, int) else None
    if not delivered:
        attempts = _claimed_attempts(doc)
        if attempts >= MAX_CLAIM_ATTEMPTS:
            write_receipt(
                root,
                _terminal_receipt(
                    event,
                    registration,
                    status="failed",
                    note="delivery_failed",
                    delivered=False,
                    verifier_returncode=returncode,
                    verifier_stdout=text,
                ),
            )
            _move_claimed(root, event_id, "failed", doc)
            return
        doc["claim_attempts"] = attempts + 1
        claimed = root / "inbox" / "claimed" / f"{event_id}.json"
        claimed.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return
    write_receipt(
        root,
        _terminal_receipt(
            event,
            registration,
            status="completed",
            note="verified",
            delivered=True,
            verifier_returncode=returncode,
            verifier_stdout=text,
        ),
    )
    _move_claimed(root, event_id, "completed", doc)


def _terminal_receipt(
    event: Mapping[str, Any],
    registration: Mapping[str, Any],
    *,
    status: str,
    note: str,
    delivered: bool = False,
    verifier_returncode: int | None = None,
    verifier_stdout: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "task_id": event.get("task_id"),
        "event_id": event.get("event_id"),
        "status": status,
        "controller_result": event.get("controller_result"),
        "note": note,
        "delivered": delivered,
        "verifier_returncode": verifier_returncode,
        "thread_key": registration.get("thread_key"),
        "created_at": _utcnow_iso(),
    }
    if verifier_stdout is not None:
        record["verifier_stdout"] = _bound_verifier_stdout(verifier_stdout)
    return record
