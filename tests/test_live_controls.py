"""Focused tests for out-of-band Telegram controls and menu sync."""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tg_cli_relay.providers.process_control import (
    clear_interrupt, interruptible_run, request_interrupt,
)


@pytest.mark.parametrize("backend", ["cursor", "claude", "codex", "opencode"])
def test_force_interrupts_and_prioritizes_same_main_session(monkeypatch, tmp_path: Path, backend):
    from tg_cli_relay import telegram_bot as bot
    from test_task_isolation import FakeMessage, _env, _reset, _update

    _env(monkeypatch, tmp_path, backend=backend)
    monkeypatch.setenv("TGR_CODEX_TRANSPORT", "cli")
    original = FakeMessage(text="原本等候的對話", chat_id=38, message_id=31)
    correction = FakeMessage(text="/force 請停止修改 A，改成 B", chat_id=38, message_id=32)

    async def run():
        _reset(bot)
        bot._ACTIVE_PARENT_BACKENDS["private:38"] = backend
        bot._ACTIVE_RELAY_THREADS.add("private:38")
        waiting = bot.QueuedTurn(
            msg=original, key="private:38", backend=backend,
            prompt=original.text, store_path=tmp_path / "sessions.sqlite3",
            attachment_config=bot._attachment_config(), route=bot.Route.DIRECT,
        )
        bot._PENDING_TURNS["private:38"] = deque([waiting])
        try:
            with patch.object(bot, "_signal_main_stop", new_callable=AsyncMock) as stop, patch.object(
                bot, "_schedule_queue_drain"
            ) as drain:
                await bot._cmd_force(_update(correction, 38), SimpleNamespace(args=["請停止修改", "A，改成", "B"]))
                stop.assert_awaited_once_with("private:38", backend)
                queue = bot._PENDING_TURNS["private:38"]
                assert len(queue) == 2
                assert queue[0].prompt == "請停止修改 A，改成 B"
                assert queue[0].backend == backend
                assert queue[0].kind == "parent"
                assert queue[1] is waiting
                drain.assert_called_once_with("private:38")
                assert "最前面" in correction.sent[-1].text
        finally:
            _reset(bot)
            bot._ACTIVE_PARENT_BACKENDS.clear()
    asyncio.run(run())


def test_native_codex_steer_keeps_running_turn(monkeypatch, tmp_path: Path):
    from tg_cli_relay import telegram_bot as bot
    from test_task_isolation import FakeMessage, _env, _reset, _update

    _env(monkeypatch, tmp_path, backend="codex")
    monkeypatch.setenv("TGR_CODEX_TRANSPORT", "app-server")
    msg = FakeMessage(text="/force 先不要動檔案", chat_id=39, message_id=32)

    async def run():
        _reset(bot)
        bot._ACTIVE_PARENT_BACKENDS["private:39"] = "codex"
        bot._ACTIVE_RELAY_THREADS.add("private:39")
        try:
            with patch.object(bot, "steer_codex_turn", return_value=True) as steer, patch.object(
                bot, "_signal_main_stop", new_callable=AsyncMock
            ) as stop:
                await bot._cmd_force(_update(msg, 39), SimpleNamespace(args=["先不要動檔案"]))
                steer.assert_called_once()
                stop.assert_not_awaited()
                assert "private:39" not in bot._PENDING_TURNS
                assert "即時" in msg.sent[-1].text
        finally:
            _reset(bot)
            bot._ACTIVE_PARENT_BACKENDS.clear()
    asyncio.run(run())


def test_stop_does_not_enqueue_or_auto_retry(monkeypatch, tmp_path: Path):
    from tg_cli_relay import telegram_bot as bot
    from test_task_isolation import FakeMessage, _env, _reset, _update

    _env(monkeypatch, tmp_path, backend="claude")
    msg = FakeMessage(text="/stop", chat_id=40, message_id=32)

    async def run():
        _reset(bot)
        bot._ACTIVE_PARENT_BACKENDS["private:40"] = "claude"
        bot._ACTIVE_RELAY_THREADS.add("private:40")
        try:
            with patch.object(bot, "_signal_main_stop", new_callable=AsyncMock) as stop:
                await bot._cmd_stop(_update(msg, 40), SimpleNamespace(args=[]))
                stop.assert_awaited_once_with("private:40", "claude")
                assert "private:40" not in bot._PENDING_TURNS
                assert "不會自動接續" in msg.sent[-1].text
        finally:
            _reset(bot)
            bot._ACTIVE_PARENT_BACKENDS.clear()
    asyncio.run(run())


def test_telegram_menu_is_refreshed_on_startup():
    from tg_cli_relay import telegram_bot as bot
    app = SimpleNamespace(bot=SimpleNamespace(set_my_commands=AsyncMock()))

    asyncio.run(bot._refresh_telegram_commands(app))

    app.bot.set_my_commands.assert_awaited_once()
    commands = app.bot.set_my_commands.await_args.args[0]
    names = {c.command for c in commands}
    assert {"force", "stop", "task", "provider", "model", "help"}.issubset(names)
    assert len(names) == len(commands)


def test_real_cli_process_is_interruptible():
    """Stopping an active main CLI must actually release the waiting thread."""
    started = threading.Event()
    result = []

    def run():
        started.set()
        result.append(interruptible_run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            thread_key="test:interruptible",
        ).returncode)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    started.wait(2)
    try:
        # The request latches even if it races with Popen registration.
        request_interrupt("test:interruptible")
        thread.join(6)
        assert not thread.is_alive(), "interrupted process still blocked the session"
        assert len(result) == 1 and result[0] != 0
    finally:
        request_interrupt("test:interruptible")
        clear_interrupt("test:interruptible")
