"""Regression for replies to the ordinary Telegram parent conversation."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from tg_cli_relay.providers.base import RunResult
from tg_cli_relay.session_store import SessionStore
from tg_cli_relay.task_registry import TaskRegistry
from test_task_isolation import FakeMessage, _drain, _env, _reset, _update


def test_parent_reply_uses_main_session_with_existing_tasks(monkeypatch, tmp_path):
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, backend="codex")
    db = tmp_path / "sessions.sqlite3"
    registry = TaskRegistry(db)
    calls = []

    def relay(**kw):
        calls.append((kw["thread_key"], kw["backend"], kw["prompt"]))
        return RunResult("父對話回答", "", 0)

    async def run():
        _reset(bot)
        try:
            with patch.object(bot, "relay_turn", side_effect=relay):
                original = FakeMessage(text="主對話原始問題", chat_id=101, message_id=100)
                await bot._on_message(_update(original, 101), SimpleNamespace())
                assert calls[-1][0] == "private:101"
                assert registry.get_parent_message_backend("private:101", 100) == "codex"
                assert original.sent
                bot_answer = original.sent[-1]
                assert registry.get_parent_message_backend(
                    "private:101", bot_answer.message_id
                ) == "codex"

                task = registry.create_task(
                    parent_key="private:101", backend="codex", title="task A",
                    status="completed",
                )
                assert registry.focused_task("private:101").task_id == task.task_id

                for reply_id in (original.message_id, bot_answer.message_id):
                    follow = FakeMessage(
                        text="剛剛那個問題再改一下", chat_id=101,
                        message_id=200 + reply_id, reply_to=reply_id,
                    )
                    await bot._on_message(_update(follow, 101), SimpleNamespace())
                    await _drain(bot)
                    assert calls[-1][0:2] == ("private:101", "codex")
                    assert "剛剛那個問題再改一下" in calls[-1][2]
                    assert str(reply_id) in calls[-1][2]
                    assert registry.get_parent_message_backend(
                        "private:101", follow.message_id
                    ) == "codex"
                assert registry.focused_task("private:101").task_id == task.task_id
        finally:
            _reset(bot)

    asyncio.run(run())


def test_unmapped_old_parent_reply_stays_safe(monkeypatch, tmp_path):
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, backend="codex")
    registry = TaskRegistry(tmp_path / "sessions.sqlite3")
    registry.create_task(parent_key="private:44", backend="codex", title="A")
    calls = []

    async def run():
        _reset(bot)
        try:
            with patch.object(
                bot, "relay_turn",
                side_effect=lambda **kw: calls.append(kw) or RunResult("ok", "", 0),
            ):
                msg = FakeMessage(
                    text="舊版沒有綁定的訊息", chat_id=44, message_id=100,
                    reply_to=11,
                )
                await bot._on_message(_update(msg, 44), SimpleNamespace())
                assert calls
                assert calls[0]["thread_key"] == "private:44"
                assert "telegram_message_id" in calls[0]["prompt"]
                assert registry.get_task_for_message("private:44", 100) is None
        finally:
            _reset(bot)

    asyncio.run(run())


def test_quote_content_is_sent_to_primary_agent(monkeypatch, tmp_path):
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, backend="claude")
    called = []

    async def run():
        _reset(bot)
        try:
            with patch.object(bot, "relay_turn", side_effect=lambda **kw: (
                called.append(kw) or RunResult("ok", "", 0)
            )):
                msg = FakeMessage(text="只補錯誤，不要重做", chat_id=4, message_id=13, reply_to=12)
                msg.reply_to_message.text = "原來的 API 工作"
                await bot._on_message(_update(msg, 4), SimpleNamespace())
                await _drain(bot)
        finally:
            _reset(bot)

    asyncio.run(run())
    assert len(called) == 1
    assert called[0]["thread_key"] == "private:4"
    assert "原來的 API 工作" in called[0]["prompt"]
    assert "只補錯誤，不要重做" in called[0]["prompt"]


def test_parent_binding_survives_restart_and_is_topic_scoped(tmp_path):
    db = tmp_path / "sessions.sqlite3"
    SessionStore(db)
    registry = TaskRegistry(db)
    registry.bind_parent_message(
        parent_key="topic:1:11", message_id=99, backend="codex"
    )
    reopened = TaskRegistry(db)
    assert reopened.get_parent_message_backend("topic:1:11", 99) == "codex"
    assert reopened.get_parent_message_backend("topic:1:12", 99) is None
