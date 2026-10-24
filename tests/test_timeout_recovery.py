"""Timeout recovery only re-prompts the main Agent; Relay never dispatches workers."""
from __future__ import annotations

import asyncio
import threading
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tg_cli_relay.providers.base import RunResult
from tg_cli_relay.session_store import SessionStore
from test_task_isolation import FakeMessage, _env, _reset, _update


@pytest.mark.parametrize("backend", ["cursor", "codex", "claude", "opencode"])
def test_timeout_recovery_prioritized_and_pending_user_messages_continue(
    monkeypatch, tmp_path: Path, backend: str
):
    from tg_cli_relay import telegram_bot as bot
    _env(monkeypatch, tmp_path, backend=backend)
    started, release = threading.Event(), threading.Event()
    calls: list[dict] = []

    def relay(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            started.set()
            release.wait(4)
            return RunResult("部分結果", "turn exceeded limit", 124)
        return RunResult("完成", "", 0)

    async def scenario():
        _reset(bot)
        bot._ACTIVE_PARENT_BACKENDS.clear()
        bot._ACTIVE_PARENT_REQUESTS.clear()
        try:
            with patch.object(bot, "relay_turn", side_effect=relay):
                first = FakeMessage(text="請修補 API 然後測試", chat_id=51, message_id=100)
                running = asyncio.create_task(bot._on_message(_update(first, 51), SimpleNamespace()))
                assert await asyncio.to_thread(started.wait, 2)
                later = FakeMessage(text="另一個問題", chat_id=51, message_id=101)
                await bot._on_message(_update(later, 51), SimpleNamespace())
                assert "等待佇列" in later.sent[-1].text
                release.set()
                await running
                while bot._BACKGROUND_TASKS:
                    await asyncio.gather(*tuple(bot._BACKGROUND_TASKS), return_exceptions=True)
        finally:
            _reset(bot)
            bot._ACTIVE_PARENT_BACKENDS.clear()
            bot._ACTIVE_PARENT_REQUESTS.clear()

    asyncio.run(scenario())
    assert len(calls) == 3
    assert all(c["thread_key"] == "private:51" for c in calls)
    assert all(c["backend"] == backend for c in calls)
    assert "Telegram message ID 100" in calls[1]["prompt"]
    assert "請修補 API 然後測試" in calls[1]["prompt"]
    assert "knowledge base" in calls[1]["prompt"]
    assert calls[2]["prompt"] == "另一個問題"


def test_recovery_turn_timeout_does_not_repeat_and_queue_drains(monkeypatch, tmp_path: Path):
    from tg_cli_relay import telegram_bot as bot
    _env(monkeypatch, tmp_path, backend="codex")
    started, release = threading.Event(), threading.Event()
    calls = []

    def relay(**kwargs):
        calls.append(kwargs["prompt"])
        if len(calls) == 1:
            started.set()
            release.wait(4)
        if len(calls) <= 2:
            return RunResult("", "timeout", 124)
        return RunResult("ok", "", 0)

    async def scenario():
        _reset(bot)
        bot._ACTIVE_PARENT_BACKENDS.clear()
        bot._ACTIVE_PARENT_REQUESTS.clear()
        try:
            with patch.object(bot, "relay_turn", side_effect=relay):
                first = FakeMessage(text="耗時工作", chat_id=52, message_id=10)
                running = asyncio.create_task(bot._on_message(_update(first, 52), SimpleNamespace()))
                assert await asyncio.to_thread(started.wait, 2)
                later = FakeMessage(text="查一下狀態", chat_id=52, message_id=11)
                await bot._on_message(_update(later, 52), SimpleNamespace())
                release.set()
                await running
                while bot._BACKGROUND_TASKS:
                    await asyncio.gather(*tuple(bot._BACKGROUND_TASKS), return_exceptions=True)
        finally:
            _reset(bot)
            bot._ACTIVE_PARENT_BACKENDS.clear()
            bot._ACTIVE_PARENT_REQUESTS.clear()

    asyncio.run(scenario())
    assert len(calls) == 3
    assert calls[0] == "耗時工作"
    assert "automatic timeout recovery" in calls[1]
    assert calls[2] == "查一下狀態"


def test_manual_stop_suppresses_auto_recovery_and_releases_queue(monkeypatch, tmp_path: Path):
    from tg_cli_relay import telegram_bot as bot
    from tg_cli_relay.providers.process_control import request_interrupt
    _env(monkeypatch, tmp_path, backend="cursor")
    started, release = threading.Event(), threading.Event()
    calls = []

    def relay(**kwargs):
        calls.append(kwargs["prompt"])
        if len(calls) == 1:
            started.set()
            release.wait(4)
            return RunResult("", "interrupted", 124)
        return RunResult("done", "", 0)

    async def scenario():
        _reset(bot)
        bot._ACTIVE_PARENT_BACKENDS.clear()
        bot._ACTIVE_PARENT_REQUESTS.clear()
        try:
            with patch.object(bot, "relay_turn", side_effect=relay):
                first = FakeMessage(text="錯誤指令", chat_id=53, message_id=20)
                running = asyncio.create_task(bot._on_message(_update(first, 53), SimpleNamespace()))
                assert await asyncio.to_thread(started.wait, 2)
                later = FakeMessage(text="之後要處理的訊息", chat_id=53, message_id=21)
                await bot._on_message(_update(later, 53), SimpleNamespace())
                stop = FakeMessage(text="/stop", chat_id=53, message_id=22)
                async def fake_stop(key, backend):
                    request_interrupt(key)
                with patch.object(bot, "_signal_main_stop", fake_stop):
                    await bot._cmd_stop(_update(stop, 53), SimpleNamespace(args=[]))
                release.set()
                await running
                while bot._BACKGROUND_TASKS:
                    await asyncio.gather(*tuple(bot._BACKGROUND_TASKS), return_exceptions=True)
        finally:
            _reset(bot)
            bot._ACTIVE_PARENT_BACKENDS.clear()
            bot._ACTIVE_PARENT_REQUESTS.clear()
    asyncio.run(scenario())
    assert calls == ["錯誤指令", "之後要處理的訊息"]


def test_background_task_at_front_does_not_starve_main_turn(monkeypatch, tmp_path: Path):
    from tg_cli_relay import telegram_bot as bot
    _env(monkeypatch, tmp_path, backend="claude")
    key = "private:54"
    calls = []

    async def scenario():
        _reset(bot)
        bot._ACTIVE_PARENT_BACKENDS.clear()
        bot._ACTIVE_PARENT_REQUESTS.clear()
        try:
            first = FakeMessage(text="獨立的顯式任務", chat_id=54, message_id=30)
            later = FakeMessage(text="一般訊息", chat_id=54, message_id=31)
            opts = dict(key=key, backend="claude", store_path=tmp_path / "sessions.sqlite3",
                        attachment_config=bot._attachment_config(), route=bot.Route.DIRECT)
            explicit = bot.QueuedTurn(msg=first, prompt=first.text, kind="new_task", **opts)
            ordinary = bot.QueuedTurn(msg=later, prompt=later.text, **opts)
            bot._PENDING_TURNS[key] = deque([explicit, ordinary])
            bot._ACTIVE_JOBS[key] = bot.RelayJob(
                job_id="existing", parent_key=key, worker_key="worker",
                backend="claude", started_at=0,
            )
            with patch.object(bot, "relay_turn", side_effect=lambda **kw: (
                calls.append(kw["prompt"]) or RunResult("ok", "", 0)
            )):
                await bot._drain_thread_queue(key)
            assert list(bot._PENDING_TURNS[key]) == [explicit]
        finally:
            _reset(bot)
            bot._ACTIVE_PARENT_BACKENDS.clear()
            bot._ACTIVE_PARENT_REQUESTS.clear()
    asyncio.run(scenario())
    assert calls == ["一般訊息"]


@pytest.mark.parametrize("backend", ["cursor", "codex", "claude", "opencode"])
def test_relay_provider_timeout_is_same_for_all_backends(tmp_path: Path, monkeypatch, backend):
    from tg_cli_relay import relay
    monkeypatch.setenv("TGR_TURN_TIMEOUT", "600")
    monkeypatch.setenv("TGR_CURSOR_TIMEOUT", "60")
    monkeypatch.setenv("TGR_CLAUDE_TIMEOUT", "1800")
    monkeypatch.setenv("TGR_CODEX_APP_TURN_TIMEOUT", "7200")
    monkeypatch.setenv("TGR_CODEX_TRANSPORT", "app-server")
    store = SessionStore(tmp_path / "sessions.sqlite3")
    store.upsert("private:77", backend, "previous", workspace=str(tmp_path))
    if backend == "cursor":
        cls = "CursorAgentProvider"
    elif backend == "claude":
        cls = "ClaudeCliProvider"
    elif backend == "opencode":
        cls = "OpencodeCliProvider"
    else:
        cls = "CodexAppServerProvider"
    with patch.object(relay, cls) as provider:
        provider.return_value.run_turn.return_value = RunResult("ok", "", 0, session_id="previous")
        relay.relay_turn(
            thread_key="private:77", backend=backend, prompt="一個工作",
            store=store, workspace=str(tmp_path),
        )
    assert provider.call_args.kwargs["timeout_seconds"] == 600
