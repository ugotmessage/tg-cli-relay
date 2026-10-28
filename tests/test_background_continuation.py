"""Provider→handoff→queue→controller→delivery with fake executables and temp DBs."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tg_cli_relay.background_callback import (
    CONTINUATION_KIND,
    accept_handoff_marker,
    continuation_message_id,
    receive_event_bytes,
    scan_once,
)
from tg_cli_relay.durable_queue import DurableQueue
from tg_cli_relay.providers.codex_cli import CodexCliProvider
from tg_cli_relay.session_store import SessionStore
from test_background_callback import SENTINEL_SECRET, _callback_env, _signed_event
from test_task_isolation import FakeMessage, _env, _reset, _update


def _write_fake_codex(path: Path) -> None:
    path.write_text(
        "#!%s\n" % sys.executable
        + r"""
import json, os, sys, time
if "--warmup" in sys.argv:
    sys.exit(0)
log = os.environ.get("FAKE_CODEX_LOG")
prompt = sys.argv[-1]
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt}) + "\n")
thread = os.environ.get("FAKE_SESSION", "sess-handoff")
sys.stdout.write(json.dumps({"type": "thread.started", "thread_id": thread}) + "\n")
sys.stdout.flush()
mode = os.environ.get("FAKE_CODEX_MODE", "reply")
if "Relay background-worker continuation" in prompt:
    text = os.environ.get("FAKE_CONTINUATION_REPLY", "續行完成")
    sys.stdout.write(json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": text},
    }) + "\n")
    sys.stdout.flush()
    sys.exit(0)
if mode == "handoff-hang":
    marker = os.environ["FAKE_MARKER"]
    sys.stdout.write(json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "已送出"},
    }) + "\n")
    sys.stdout.flush()
    sys.stdout.write(json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": marker},
    }) + "\n")
    sys.stdout.flush()
    time.sleep(30)
    sys.exit(0)
if mode == "timeout":
    time.sleep(30)
    sys.exit(0)
if mode == "quoted-marker":
    marker = os.environ["FAKE_MARKER"]
    sys.stdout.write(json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "note " + marker},
    }) + "\n")
    sys.stdout.flush()
    time.sleep(30)
    sys.exit(0)
if mode == "tool-output":
    marker = os.environ["FAKE_MARKER"]
    sys.stdout.write(json.dumps({
        "type": "item.completed",
        "item": {"type": "command_execution", "text": marker},
    }) + "\n")
    sys.stdout.flush()
    sys.stdout.write(json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "普通回覆"},
    }) + "\n")
    sys.stdout.flush()
    sys.exit(0)
text = os.environ.get("FAKE_REPLY", "一般回覆")
sys.stdout.write(json.dumps({
    "type": "item.completed",
    "item": {"type": "agent_message", "text": text},
}) + "\n")
sys.stdout.flush()
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _full_env(monkeypatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    _env(monkeypatch, tmp_path, backend="codex")
    state, secret = _callback_env(monkeypatch, tmp_path)
    fake = tmp_path / "fake-codex"
    _write_fake_codex(fake)
    monkeypatch.setenv("TGR_CODEX_BIN", str(fake))
    monkeypatch.setenv("TGR_CODEX_TRANSPORT", "cli")
    monkeypatch.setenv("TGR_TURN_TIMEOUT", "2")
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "codex.log"))
    monkeypatch.setenv("FAKE_SESSION", "sess-handoff")
    return state, secret, fake


def _handoff_file(tmp_path: Path, name: str = "handoff.md") -> Path:
    path = tmp_path / name
    path.write_text("durable worker handoff", encoding="utf-8")
    return path


def _codex_log(tmp_path: Path) -> list[dict]:
    path = tmp_path / "codex.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_streaming_handoff_detaches_without_timeout_and_preserves_resume(tmp_path, monkeypatch):
    fake = tmp_path / "fake-codex"
    _write_fake_codex(fake)
    handoff = _handoff_file(tmp_path)
    marker = f"TGR_BACKGROUND_HANDOFF:task-stop:{handoff}"
    monkeypatch.setenv("FAKE_CODEX_MODE", "handoff-hang")
    monkeypatch.setenv("FAKE_MARKER", marker)
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "codex.log"))
    accepted: list[tuple[str, str]] = []

    def on_handoff(task_id: str, path: str) -> bool:
        accepted.append((task_id, path))
        return True

    started = time.monotonic()
    result = CodexCliProvider(codex_bin=str(fake), timeout_seconds=8).run_turn(
        workspace=str(tmp_path),
        session_id=None,
        prompt="請派工",
        on_progress=lambda _: None,
        on_handoff=on_handoff,
        thread_key="private:handoff-stop",
    )
    assert time.monotonic() - started < 5
    assert result.handoff_detached is True
    assert result.returncode == 0
    assert result.session_id == "sess-handoff"
    assert accepted == [("task-stop", str(handoff))]
    assert "Codex CLI turn exceeded timeout" not in (result.stderr or "")

    monkeypatch.setenv("FAKE_CODEX_MODE", "reply")
    monkeypatch.setenv("FAKE_REPLY", "續上一段")
    resumed = CodexCliProvider(codex_bin=str(fake), timeout_seconds=8).run_turn(
        workspace=str(tmp_path),
        session_id=result.session_id,
        prompt="繼續",
        thread_key="private:handoff-resume",
    )
    log_lines = _codex_log(tmp_path)
    assert any("resume" in entry["argv"] and "sess-handoff" in entry["argv"] for entry in log_lines)
    assert resumed.returncode == 0
    assert resumed.handoff_detached is False
    assert "續上一段" in resumed.stdout


def test_streaming_timeout_without_marker_is_124(tmp_path, monkeypatch):
    fake = tmp_path / "fake-codex"
    _write_fake_codex(fake)
    subprocess.run([str(fake), "--warmup"], check=True, timeout=5)
    monkeypatch.setenv("FAKE_CODEX_MODE", "timeout")
    result = CodexCliProvider(codex_bin=str(fake), timeout_seconds=0.4).run_turn(
        workspace=str(tmp_path),
        session_id=None,
        prompt="會逾時",
        on_progress=lambda _: None,
        on_handoff=lambda *_: True,
        thread_key="private:timeout-only",
    )
    assert result.handoff_detached is False
    assert result.returncode == 124
    assert result.session_id == "sess-handoff"


@pytest.mark.parametrize("mode", ["quoted-marker", "tool-output"])
def test_quoted_or_tool_marker_does_not_detach(tmp_path, monkeypatch, mode):
    fake = tmp_path / "fake-codex"
    _write_fake_codex(fake)
    handoff = _handoff_file(tmp_path)
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)
    monkeypatch.setenv("FAKE_MARKER", f"TGR_BACKGROUND_HANDOFF:task-bad:{handoff}")
    calls: list[tuple[str, str]] = []
    timeout = 0.4 if mode == "quoted-marker" else 8
    result = CodexCliProvider(codex_bin=str(fake), timeout_seconds=timeout).run_turn(
        workspace=str(tmp_path),
        session_id=None,
        prompt="不要拆離",
        on_progress=lambda _: None,
        on_handoff=lambda task_id, path: calls.append((task_id, path)) or True,
        thread_key=f"private:{mode}",
    )
    assert calls == []
    assert result.handoff_detached is False
    if mode == "quoted-marker":
        assert result.returncode == 124
    else:
        assert result.returncode == 0
        assert "普通回覆" in result.stdout


def test_submission_releases_while_worker_pending_and_later_user_completes(monkeypatch, tmp_path):
    from tg_cli_relay import telegram_bot as bot

    _full_env(monkeypatch, tmp_path)
    handoff = _handoff_file(tmp_path)
    marker = f"TGR_BACKGROUND_HANDOFF:task-bg:{handoff}"
    monkeypatch.setenv("FAKE_CODEX_MODE", "handoff-hang")
    monkeypatch.setenv("FAKE_MARKER", marker)

    first = FakeMessage(text="請派背景工作", chat_id=7, message_id=20)
    first.from_user = SimpleNamespace(id=99)
    later = FakeMessage(text="另一個問題", chat_id=7, message_id=21)
    later.from_user = SimpleNamespace(id=99)

    async def scenario():
        _reset(bot)
        bot._REVIEW_REQUIRED_THREADS.clear()
        try:
            await bot._on_message(_update(first, 7), SimpleNamespace())
            monkeypatch.setenv("FAKE_CODEX_MODE", "reply")
            monkeypatch.setenv("FAKE_REPLY", "後面這則先完成")
            await bot._on_message(_update(later, 7), SimpleNamespace())
            await asyncio.gather(*tuple(bot._BACKGROUND_TASKS), return_exceptions=True)
        finally:
            _reset(bot)
            bot._REVIEW_REQUIRED_THREADS.clear()

    asyncio.run(scenario())
    first_texts = [getattr(s, "text", "") or "" for s in first.sent]
    later_texts = [getattr(s, "text", "") or "" for s in later.sent]
    assert any("已登記背景工作" in text for text in first_texts)
    assert all(marker not in text for text in first_texts)
    assert any("後面這則先完成" in text for text in later_texts)
    store = SessionStore(tmp_path / "sessions.sqlite3")
    assert store.get("private:7", "codex") == "sess-handoff"
    assert DurableQueue(tmp_path / "sessions.sqlite3").open_items("private:7") == []


def test_handoff_does_not_queue_timeout_recovery(monkeypatch, tmp_path):
    from tg_cli_relay import telegram_bot as bot

    _full_env(monkeypatch, tmp_path)
    monkeypatch.setenv("TGR_TURN_TIMEOUT", "8")
    handoff = _handoff_file(tmp_path)
    marker = f"TGR_BACKGROUND_HANDOFF:task-norecovery:{handoff}"
    monkeypatch.setenv("FAKE_CODEX_MODE", "handoff-hang")
    monkeypatch.setenv("FAKE_MARKER", marker)
    msg = FakeMessage(text="派工後應釋放", chat_id=7, message_id=30)
    msg.from_user = SimpleNamespace(id=99)

    async def scenario():
        _reset(bot)
        bot._REVIEW_REQUIRED_THREADS.clear()
        try:
            await bot._on_message(_update(msg, 7), SimpleNamespace())
            await asyncio.gather(*tuple(bot._BACKGROUND_TASKS), return_exceptions=True)
        finally:
            _reset(bot)
            bot._REVIEW_REQUIRED_THREADS.clear()

    asyncio.run(scenario())
    prompts = [entry["prompt"] for entry in _codex_log(tmp_path)]
    assert prompts
    assert all("automatic timeout recovery" not in prompt for prompt in prompts)
    rows = DurableQueue(tmp_path / "sessions.sqlite3").open_items("private:7")
    assert all(row.kind != "timeout_recovery" for row in rows)


def test_terminal_completion_continues_original_session_once(monkeypatch, tmp_path):
    from tg_cli_relay import telegram_bot as bot

    state, _, _ = _full_env(monkeypatch, tmp_path)
    handoff = _handoff_file(tmp_path)
    marker = f"TGR_BACKGROUND_HANDOFF:task-1:{handoff}"
    monkeypatch.setenv("FAKE_CODEX_MODE", "handoff-hang")
    monkeypatch.setenv("FAKE_MARKER", marker)
    submit = FakeMessage(text="請派背景工作", chat_id=7, message_id=40)
    submit.from_user = SimpleNamespace(id=99)
    busy_main = FakeMessage(text="先處理我", chat_id=7, message_id=41)
    busy_main.from_user = SimpleNamespace(id=99)
    recorded: list[str] = []
    bot_api = SimpleNamespace(
        send_message=AsyncMock(
            side_effect=lambda **kw: recorded.append(kw.get("text", "") or "")
            or SimpleNamespace(message_id=900)
        ),
        send_chat_action=AsyncMock(),
        send_document=AsyncMock(),
    )

    async def scenario():
        _reset(bot)
        bot._REVIEW_REQUIRED_THREADS.clear()
        try:
            await bot._on_message(_update(submit, 7), SimpleNamespace())
            event = _signed_event(
                SENTINEL_SECRET,
                task_id="task-1",
                handoff_path=str(handoff),
                snapshot="worker handoff",
            )
            assert receive_event_bytes(json.dumps(event).encode("utf-8")).reason == "accepted"
            monkeypatch.setenv("FAKE_CODEX_MODE", "reply")
            monkeypatch.setenv("FAKE_REPLY", "主對話回覆")
            monkeypatch.setenv("FAKE_CONTINUATION_REPLY", "背景續行一次")
            running = asyncio.create_task(bot._on_message(_update(busy_main, 7), SimpleNamespace()))
            await asyncio.sleep(0.05)
            await scan_once(
                active_thread_keys={"private:7"},
                deliver=lambda *_: asyncio.sleep(0),
                schedule_continuation=lambda reg, ev: bot._schedule_background_continuation(
                    bot_api, reg, ev
                ),
            )
            assert DurableQueue(tmp_path / "sessions.sqlite3").lookup(
                thread_key="private:7",
                message_id=continuation_message_id(event["event_id"]),
                kind=CONTINUATION_KIND,
            ) is None
            await running
            await scan_once(
                active_thread_keys=set(),
                deliver=lambda *_: asyncio.sleep(0),
                schedule_continuation=lambda reg, ev: bot._schedule_background_continuation(
                    bot_api, reg, ev
                ),
            )
            while bot._BACKGROUND_TASKS:
                await asyncio.gather(*tuple(bot._BACKGROUND_TASKS), return_exceptions=True)
            assert receive_event_bytes(json.dumps(event).encode("utf-8")).reason == "replay"
            await scan_once(
                active_thread_keys=set(),
                deliver=lambda *_: asyncio.sleep(0),
                schedule_continuation=lambda reg, ev: bot._schedule_background_continuation(
                    bot_api, reg, ev
                ),
            )
            while bot._BACKGROUND_TASKS:
                await asyncio.gather(*tuple(bot._BACKGROUND_TASKS), return_exceptions=True)
        finally:
            _reset(bot)
            bot._REVIEW_REQUIRED_THREADS.clear()

    asyncio.run(scenario())
    continuation_prompts = [
        entry["prompt"]
        for entry in _codex_log(tmp_path)
        if "Relay background-worker continuation" in entry["prompt"]
    ]
    assert len(continuation_prompts) == 1
    assert "task-1" in continuation_prompts[0]
    assert "worker handoff" in continuation_prompts[0]
    assert any("背景續行一次" in text for text in recorded)
    assert any("主對話回覆" in (getattr(s, "text", "") or "") for s in busy_main.sent)
    receipt = json.loads((state / "receipts" / "task-1.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "continuation_queued"


def test_uncertain_continuation_does_not_block_or_rerun(monkeypatch, tmp_path):
    from tg_cli_relay import telegram_bot as bot

    _full_env(monkeypatch, tmp_path)
    handoff = _handoff_file(tmp_path)
    msg = FakeMessage(text="hi", chat_id=7, message_id=1)
    msg.from_user = SimpleNamespace(id=99)
    accept_handoff_marker("task-u", str(handoff), msg=msg, thread_key="private:7")
    event = _signed_event(SENTINEL_SECRET, task_id="task-u", handoff_path=str(handoff))
    assert receive_event_bytes(json.dumps(event).encode("utf-8")).reason == "accepted"
    db = tmp_path / "sessions.sqlite3"
    ledger = DurableQueue(db)
    qid = ledger.add(
        thread_key="private:7",
        backend="codex",
        prompt="Relay background-worker continuation. stale",
        kind=CONTINUATION_KIND,
        chat_id=7,
        message_id=continuation_message_id(event["event_id"]),
        topic_id=None,
        task_id="task-u",
    )
    assert ledger.start(qid)
    assert ledger.mark_interrupted_uncertain() == 1
    assert ledger.review_threads() == set()

    later = FakeMessage(text="新的普通訊息", chat_id=7, message_id=55)
    later.from_user = SimpleNamespace(id=99)
    restored_bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=902)),
        send_chat_action=AsyncMock(),
        send_document=AsyncMock(),
    )

    async def scenario():
        _reset(bot)
        bot._REVIEW_REQUIRED_THREADS.clear()
        try:
            monkeypatch.setenv("FAKE_CODEX_MODE", "reply")
            monkeypatch.setenv("FAKE_REPLY", "普通訊息完成")
            await bot._restore_durable_queue(restored_bot)
            await bot._on_message(_update(later, 7), SimpleNamespace())
            while bot._BACKGROUND_TASKS:
                await asyncio.gather(*tuple(bot._BACKGROUND_TASKS), return_exceptions=True)
        finally:
            _reset(bot)
            bot._REVIEW_REQUIRED_THREADS.clear()

    asyncio.run(scenario())
    later_texts = [getattr(s, "text", "") or "" for s in later.sent]
    assert any("普通訊息完成" in text for text in later_texts)
    rows = DurableQueue(db).open_items("private:7")
    uncertain = [row for row in rows if row.kind == CONTINUATION_KIND]
    assert uncertain and uncertain[0].status == "needs_review"
    assert all(
        "Relay background-worker continuation" not in entry["prompt"]
        for entry in _codex_log(tmp_path)
    )


def test_invalid_signature_does_not_enqueue_continuation(monkeypatch, tmp_path):
    from tg_cli_relay import telegram_bot as bot

    _full_env(monkeypatch, tmp_path)
    handoff = _handoff_file(tmp_path)
    msg = FakeMessage(text="hi", chat_id=7, message_id=1)
    msg.from_user = SimpleNamespace(id=99)
    accept_handoff_marker("task-1", str(handoff), msg=msg, thread_key="private:7")
    event = _signed_event(SENTINEL_SECRET, task_id="task-1", handoff_path=str(handoff))
    event["hmac"] = "00" * 32
    assert receive_event_bytes(json.dumps(event).encode("utf-8")).reason == "bad_signature"
    scheduled: list[str] = []

    async def scenario():
        _reset(bot)
        try:
            await scan_once(
                active_thread_keys=set(),
                deliver=lambda *_: asyncio.sleep(0),
                schedule_continuation=lambda *_: scheduled.append("nope") or asyncio.sleep(0),
            )
        finally:
            _reset(bot)

    asyncio.run(scenario())
    assert scheduled == []
    assert DurableQueue(tmp_path / "sessions.sqlite3").load_queued() == []
