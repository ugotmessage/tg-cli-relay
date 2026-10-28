"""HMS → WEB1 background callback: registration, HMAC receive, inbox consumer."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tg_cli_relay.background_callback import (
    MAX_EVENT_BYTES,
    MAX_SNAPSHOT_BYTES,
    build_verification_prompt,
    feature_enabled,
    find_handoff_markers,
    parse_handoff_marker,
    receive_event_bytes,
    receive_from_stdin,
    register_from_relay_context,
    run_fresh_verifier,
    scan_once,
    sign_event,
    write_receipt,
)
from tg_cli_relay.cli import main as cli_main
from tg_cli_relay.providers.base import RunResult
from test_task_isolation import FakeMessage, _env, _reset, _update

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import watchdog_callback_hooks  # noqa: E402


FIXED_NOW = datetime(2026, 10, 28, 3, 54, tzinfo=timezone.utc)
SENTINEL_SECRET = b"SEN_TEST_SECRET_7f3a9c2e1b88dead"


def _callback_env(monkeypatch, tmp_path: Path, *, enabled: bool = True) -> tuple[Path, Path]:
    state = tmp_path / "callback-state"
    secret = tmp_path / "callback.secret"
    secret.write_bytes(SENTINEL_SECRET)
    monkeypatch.setenv("TGR_BACKGROUND_HANDOFF", "1" if enabled else "0")
    monkeypatch.setenv("TGR_BACKGROUND_HANDOFF_STATE_ROOT", str(state))
    monkeypatch.setenv("TGR_BACKGROUND_HANDOFF_SECRET_FILE", str(secret))
    monkeypatch.setenv("TGR_BACKGROUND_HANDOFF_MAX_SKEW_SECONDS", "300")
    monkeypatch.setenv("TGR_DEFAULT_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:TESTTOKEN")
    (tmp_path / "ws").mkdir()
    monkeypatch.setattr("tg_cli_relay.background_callback._utcnow", lambda: FIXED_NOW)
    return state, secret


def _msg(chat_id: int = 7, thread_id: int | None = None) -> FakeMessage:
    msg = FakeMessage(text="hi", chat_id=chat_id, message_id=1, thread_id=thread_id)
    msg.from_user = SimpleNamespace(id=99)
    return msg


def _register(tmp_path: Path, *, task_id: str = "task-1", handoff: str | None = None) -> dict:
    path = handoff or f"/private/tmp/callback-{task_id}.md"
    return register_from_relay_context(
        task_id=task_id,
        handoff_path=path,
        msg=_msg(),
        thread_key="private:7",
    )


def _signed_event(
    secret: bytes,
    *,
    task_id: str = "task-1",
    status: str = "done",
    controller_result: str = "complete",
    event_id: str | None = None,
    occurred_at: str | None = None,
    handoff_path: str = "/private/tmp/callback-task-1.md",
    extra: dict | None = None,
    snapshot: str | None = None,
) -> dict:
    event = {
        "schema_version": 1,
        "event_id": event_id or str(uuid.uuid4()),
        "task_id": task_id,
        "status": status,
        "controller_result": controller_result,
        "controller_detail": "ok",
        "occurred_at": occurred_at or "2026-10-28T03:54:00Z",
        "handoff_path": handoff_path,
        "handoff_sha256": None,
        "handoff_size": None,
        "handoff_snapshot": snapshot,
        "artifact": None,
    }
    if extra:
        event.update(extra)
    event["hmac"] = sign_event(event, secret)
    return event


def _receive(event: dict) -> tuple[int, str]:
    data = json.dumps(event, ensure_ascii=False).encode("utf-8")
    result = receive_event_bytes(data)
    return result.code, result.reason


async def _consume(verifier, deliver, busy: set[str] | None = None) -> None:
    await scan_once(active_thread_keys=busy or set(), deliver=deliver, verifier=verifier)


def test_feature_flag_default_off(monkeypatch):
    monkeypatch.delenv("TGR_BACKGROUND_HANDOFF", raising=False)
    assert feature_enabled() is False
    monkeypatch.setenv("TGR_BACKGROUND_HANDOFF", "0")
    assert feature_enabled() is False
    monkeypatch.setenv("TGR_BACKGROUND_HANDOFF", "false")
    assert feature_enabled() is False
    monkeypatch.setenv("TGR_BACKGROUND_HANDOFF", "1")
    assert feature_enabled() is True


def test_marker_is_whole_message_only():
    assert parse_handoff_marker("TGR_BACKGROUND_HANDOFF:task-1:/private/tmp/a.md")
    assert parse_handoff_marker("note TGR_BACKGROUND_HANDOFF:task-1:/private/tmp/a.md") is None
    res = RunResult("前置\nTGR_BACKGROUND_HANDOFF:task-1:/private/tmp/a.md", "", 0)
    remaining, markers = find_handoff_markers(res)
    assert markers == []
    assert remaining is None
    segmented = RunResult(
        "x",
        "",
        0,
        stdout_segments=["步驟完成", "TGR_BACKGROUND_HANDOFF:task-1:/private/tmp/a.md"],
    )
    remaining, markers = find_handoff_markers(segmented)
    assert markers == [("task-1", "/private/tmp/a.md")]
    assert remaining == ["步驟完成"]


def test_valid_signed_event_one_verifier_one_delivery(monkeypatch, tmp_path):
    state, secret = _callback_env(monkeypatch, tmp_path)
    _register(tmp_path)
    event = _signed_event(SENTINEL_SECRET)
    assert _receive(event) == (0, "accepted")
    assert (state / "inbox" / "new" / f"{event['event_id']}.json").is_file()

    calls: list[tuple[str, str]] = []
    delivered: list[str] = []

    def verifier(*, workspace, prompt):
        calls.append((workspace, prompt))
        assert workspace == str(tmp_path / "ws")
        assert "hmac" not in prompt
        return RunResult("驗收通過", "", 0)

    async def deliver(reg, text):
        delivered.append(text)
        assert reg["chat_id"] == 7
        assert reg["thread_key"] == "private:7"

    asyncio.run(_consume(verifier, deliver))
    assert len(calls) == 1
    assert delivered == ["驗收通過"]
    receipt = json.loads((state / "receipts" / "task-1.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "completed"
    assert SENTINEL_SECRET.decode() not in json.dumps(receipt)
    asyncio.run(_consume(verifier, deliver))
    assert len(calls) == 1
    assert len(delivered) == 1


def _resign(event: dict, **fields) -> dict:
    updated = {k: v for k, v in event.items() if k != "hmac"}
    updated.update(fields)
    updated["hmac"] = sign_event(updated, SENTINEL_SECRET)
    return updated


def test_receive_rejects_without_model_or_telegram(monkeypatch, tmp_path):
    _callback_env(monkeypatch, tmp_path)
    _register(tmp_path)
    event = _signed_event(SENTINEL_SECRET)
    bad_sig = dict(event)
    bad_sig["hmac"] = "00" * 32
    assert _receive(bad_sig) == (3, "bad_signature")
    assert _receive(_resign(event, occurred_at="2026-10-28T03:40:00Z")) == (3, "stale_timestamp")
    assert _receive(event) == (0, "accepted")
    assert _receive(event) == (3, "replay")
    poisoned = _resign(event, chat_id=7)
    assert _receive(poisoned) == (3, "poisoned")
    assert _receive(_resign(event, status="pending")) == (3, "bad_status")
    assert _receive(_resign(event, status="running")) == (3, "bad_status")
    assert _receive(_resign(event, handoff_path="/private/tmp/foo/../../etc/passwd")) == (3, "traversal")
    huge = _signed_event(SENTINEL_SECRET, snapshot="x" * (MAX_SNAPSHOT_BYTES + 1), event_id=str(uuid.uuid4()))
    huge["hmac"] = "ab"
    assert _receive(huge) == (3, "oversized")
    assert _receive(_resign(event, task_id="other-task")) == (2, "unknown_task")


def test_unequal_length_hmac_is_bad_signature(monkeypatch, tmp_path):
    _callback_env(monkeypatch, tmp_path)
    _register(tmp_path)
    event = _signed_event(SENTINEL_SECRET)
    event["hmac"] = "deadbeef"
    assert _receive(event) == (3, "bad_signature")
    event["hmac"] = "xyz"
    assert _receive(event) == (3, "bad_signature")


def test_future_timestamp_and_conflict_and_symlink(monkeypatch, tmp_path):
    state, secret = _callback_env(monkeypatch, tmp_path)
    _register(tmp_path)
    future = _signed_event(SENTINEL_SECRET, occurred_at="2026-10-28T04:10:00Z")
    assert _receive(future) == (3, "future_timestamp")

    first = _signed_event(SENTINEL_SECRET, event_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert _receive(first) == (0, "accepted")
    conflict = dict(first)
    conflict["controller_detail"] = "other"
    conflict["hmac"] = sign_event(conflict, SENTINEL_SECRET)
    assert _receive(conflict) == (3, "conflict")

    target = tmp_path / "real-handoff.md"
    target.write_text("hi", encoding="utf-8")
    link = tmp_path / "link-handoff.md"
    link.symlink_to(target)
    with pytest.raises(Exception):
        register_from_relay_context(
            task_id="task-link",
            handoff_path=str(link),
            msg=_msg(),
            thread_key="private:7",
        )


def test_oversized_json_and_cli_reason_token(monkeypatch, tmp_path):
    _callback_env(monkeypatch, tmp_path)
    _register(tmp_path)
    blob = b"{" + (b"x" * (MAX_EVENT_BYTES + 10))
    result = receive_event_bytes(blob)
    assert result.code == 3
    assert result.reason == "oversized"
    event = _signed_event(SENTINEL_SECRET)
    buf, out = BytesIO(json.dumps(event).encode()), StringIO()
    assert receive_from_stdin(buf, out) == 0
    assert out.getvalue().strip() == "accepted"
    assert SENTINEL_SECRET.decode() not in out.getvalue()
    assert event["hmac"] not in out.getvalue()


def test_pending_running_never_wake_codex(monkeypatch, tmp_path):
    state, _ = _callback_env(monkeypatch, tmp_path)
    _register(tmp_path)
    event = _signed_event(SENTINEL_SECRET, status="running", controller_result="failed")
    assert _receive(event)[1] == "bad_status"
    inbox = state / "inbox" / "new" / f"{event['event_id']}.json"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(json.dumps(event), encoding="utf-8")
    calls: list[int] = []

    def verifier(**kwargs):
        calls.append(1)
        return RunResult("nope", "", 0)

    delivered: list[str] = []
    asyncio.run(_consume(verifier, lambda r, t: delivered.append(t) or asyncio.sleep(0)))
    assert calls == []
    assert delivered
    assert "獨立驗收未啟動" in delivered[0] or "未通過" in delivered[0]


def test_active_thread_defers_then_restart_recovers(monkeypatch, tmp_path):
    state, _ = _callback_env(monkeypatch, tmp_path)
    _register(tmp_path)
    event = _signed_event(SENTINEL_SECRET)
    assert _receive(event) == (0, "accepted")
    calls: list[str] = []

    def verifier(**kwargs):
        calls.append("v")
        return RunResult("ok", "", 0)

    delivered: list[str] = []

    async def deliver(reg, text):
        delivered.append(text)

    asyncio.run(_consume(verifier, deliver, busy={"private:7"}))
    assert calls == []
    assert (state / "inbox" / "new" / f"{event['event_id']}.json").is_file()
    asyncio.run(_consume(verifier, deliver, busy=set()))
    assert calls == ["v"]
    assert delivered == ["ok"]
    receipt = json.loads((state / "receipts" / "task-1.json").read_text(encoding="utf-8"))
    claimed = state / "inbox" / "claimed" / f"{event['event_id']}.json"
    if claimed.exists():
        asyncio.run(_consume(verifier, deliver))
    assert calls == ["v"]
    assert receipt["status"] == "completed"


def test_verifier_and_delivery_failures_are_bounded(monkeypatch, tmp_path):
    _callback_env(monkeypatch, tmp_path)
    _register(tmp_path, task_id="task-v")
    event = _signed_event(SENTINEL_SECRET, task_id="task-v")
    assert _receive(event) == (0, "accepted")
    calls: list[str] = []

    def verifier(**kwargs):
        calls.append("v")
        return RunResult("", "boom", 1)

    delivered: list[str] = []
    asyncio.run(_consume(verifier, lambda r, t: delivered.append(t) or asyncio.sleep(0)))
    assert calls == ["v"]
    assert any("驗收失敗" in item for item in delivered)

    _register(tmp_path, task_id="task-d")
    event2 = _signed_event(SENTINEL_SECRET, task_id="task-d")
    assert _receive(event2) == (0, "accepted")
    calls.clear()
    delivered.clear()

    def ok_verifier(**kwargs):
        calls.append("v")
        return RunResult("通過", "", 0)

    async def boom_deliver(reg, text):
        raise RuntimeError("telegram down")

    asyncio.run(_consume(ok_verifier, boom_deliver))
    assert calls == ["v"]
    asyncio.run(_consume(ok_verifier, boom_deliver))
    assert calls == ["v"]


def test_crash_after_verifier_does_not_rerun_on_restart(monkeypatch, tmp_path):
    state, _ = _callback_env(monkeypatch, tmp_path)
    _register(tmp_path)
    event = _signed_event(SENTINEL_SECRET)
    assert _receive(event) == (0, "accepted")
    calls: list[str] = []

    def verifier(**kwargs):
        calls.append("v")
        return RunResult("驗收通過", "", 0)

    real_write = write_receipt

    def write_pending_then_crash(root, record):
        if record.get("status") == "verified_pending_delivery":
            real_write(root, record)
            raise RuntimeError("crash after verifier receipt")
        real_write(root, record)

    monkeypatch.setattr("tg_cli_relay.background_callback.write_receipt", write_pending_then_crash)
    delivered: list[str] = []

    async def deliver(reg, text):
        delivered.append(text)

    with pytest.raises(RuntimeError, match="crash after verifier receipt"):
        asyncio.run(_consume(verifier, deliver))
    assert calls == ["v"]
    assert delivered == []
    pending = json.loads((state / "receipts" / "task-1.json").read_text(encoding="utf-8"))
    assert pending["status"] == "verified_pending_delivery"
    assert pending["verifier_stdout"] == "驗收通過"
    assert pending["delivered"] is False
    assert (state / "inbox" / "claimed" / f"{event['event_id']}.json").is_file()

    monkeypatch.setattr("tg_cli_relay.background_callback.write_receipt", real_write)
    asyncio.run(_consume(verifier, deliver))
    assert calls == ["v"]
    assert delivered == ["驗收通過"]
    receipt = json.loads((state / "receipts" / "task-1.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "completed"
    assert receipt["delivered"] is True
    assert not (state / "inbox" / "claimed" / f"{event['event_id']}.json").exists()
    assert (state / "inbox" / "completed" / f"{event['event_id']}.json").is_file()


def test_secret_sentinel_absent_from_logs_errors_receipts(monkeypatch, tmp_path, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    state, _ = _callback_env(monkeypatch, tmp_path)
    _register(tmp_path)
    event = _signed_event(SENTINEL_SECRET)
    event["hmac"] = "00" * 32
    code, reason = _receive(event)
    assert reason == "bad_signature"
    blob = caplog.text + reason + json.dumps(event)
    # hmac/token of the secret itself must not be echoed as a leak
    assert SENTINEL_SECRET.decode() not in caplog.text
    assert SENTINEL_SECRET.decode() not in reason
    receipt_dir = state / "receipts"
    for path in receipt_dir.glob("*.json") if receipt_dir.exists() else []:
        assert SENTINEL_SECRET.decode() not in path.read_text(encoding="utf-8")
    assert code == 3
    del blob


def test_callback_disabled_preserves_relay_behavior(monkeypatch, tmp_path):
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, backend="codex")
    _callback_env(monkeypatch, tmp_path, enabled=False)
    marker = "TGR_BACKGROUND_HANDOFF:task-1:/private/tmp/a.md"
    calls: list[str] = []
    msg = FakeMessage(text="請處理", chat_id=7, message_id=3)

    async def scenario():
        _reset(bot)
        try:
            with patch.object(bot, "relay_turn", side_effect=lambda **kw: (
                calls.append(kw["prompt"]) or RunResult(marker, "", 0)
            )):
                await bot._on_message(_update(msg, 7), SimpleNamespace())
        finally:
            _reset(bot)

    asyncio.run(scenario())
    assert calls == ["請處理"]
    assert any(marker in (getattr(s, "text", "") or "") for s in msg.sent)
    assert receive_event_bytes(b"{}").reason == "disabled"
    asyncio.run(scan_once(
        active_thread_keys=set(),
        deliver=lambda *_: asyncio.sleep(0),
        verifier=lambda **k: (_ for _ in ()).throw(AssertionError("no verifier")),
    ))


def test_marker_registers_strips_and_acks(monkeypatch, tmp_path):
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, backend="codex")
    state, _ = _callback_env(monkeypatch, tmp_path)
    marker = "TGR_BACKGROUND_HANDOFF:task-9:/private/tmp/handoff.md"
    msg = FakeMessage(text="長任務", chat_id=7, message_id=4)
    msg.from_user = SimpleNamespace(id=99)

    async def scenario():
        _reset(bot)
        try:
            with patch.object(bot, "relay_turn", return_value=RunResult(
                "done", "", 0, stdout_segments=["說明", marker]
            )):
                await bot._on_message(_update(msg, 7), SimpleNamespace())
        finally:
            _reset(bot)

    asyncio.run(scenario())
    texts = [getattr(s, "text", "") or "" for s in msg.sent]
    assert any("已登記背景工作" in t for t in texts)
    assert all(marker not in t for t in texts)
    assert any("說明" in t for t in texts)
    assert (state / "registrations").exists()
    matches = list((state / "registrations").glob("*/task-9.json"))
    assert matches


def test_watchdog_transport_failure_does_not_alter_classification():
    def enqueue(*_args):
        raise RuntimeError("ssh down")

    classified = watchdog_callback_hooks.after_terminal_callback(
        classified="success",
        terminal_path="/tmp/done.json",
        detail="ok",
        enqueue=enqueue,
        drain=lambda: None,
    )
    assert classified == "success"
    assert watchdog_callback_hooks.map_controller_result("validation", "x")[0] == (
        "worker_done_validation_failed"
    )
    assert watchdog_callback_hooks.map_controller_result("contract")[0] == "blocked_before_worker"
    assert watchdog_callback_hooks.map_controller_result("error", "boom")[0] == "failed"


def test_fresh_verifier_uses_session_none(monkeypatch, tmp_path):
    captured: dict = {}

    class FakeProvider:
        def __init__(self, **kwargs):
            pass

        def run_turn(self, **kwargs):
            captured.update(kwargs)
            return RunResult("ok", "", 0)

    monkeypatch.setattr("tg_cli_relay.providers.codex_cli.CodexCliProvider", FakeProvider)
    result = run_fresh_verifier(workspace=str(tmp_path), prompt="verify")
    assert result.stdout == "ok"
    assert captured["session_id"] is None
    assert captured["workspace"] == str(tmp_path)


def test_prompt_omits_destination_and_hmac():
    prompt = build_verification_prompt(
        {"thread_key": "private:7", "chat_id": 7, "relay_workspace": "/tmp/ws"},
        {
            "task_id": "t",
            "controller_result": "complete",
            "controller_detail": "ok",
            "handoff_path": "/private/tmp/a.md",
            "handoff_sha256": None,
            "handoff_size": None,
            "artifact": None,
            "hmac": "should-not-appear",
            "chat_id": 7,
        },
    )
    assert "should-not-appear" not in prompt
    assert "chat_id" not in prompt


def test_cli_callback_receive_disabled(monkeypatch, tmp_path):
    _callback_env(monkeypatch, tmp_path, enabled=False)
    monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=BytesIO(b"{}")))
    assert cli_main(["callback-receive"]) == 4
