"""End-to-end continuity checks: focus, follow-up routing, worker sessions."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tg_cli_relay.providers.base import RunResult
from tg_cli_relay.session_store import SessionStore
from tg_cli_relay.task_registry import TaskRegistry
from tg_cli_relay.task_router import Route, default_task_router_config, route_text


def test_task_focus_survives_registry_recreation_and_never_crosses_chat(tmp_path: Path):
    db = tmp_path / "sessions.sqlite3"
    SessionStore(db)
    registry = TaskRegistry(db)
    a = registry.create_task(parent_key="private:1", backend="codex", title="login API")
    b = registry.create_task(parent_key="private:1", backend="codex", title="website B")
    other = registry.create_task(parent_key="private:2", backend="codex", title="another user")
    assert TaskRegistry(db).focused_task("private:1").task_id == b.task_id
    assert not registry.focus_task("private:2", a.task_id)
    assert registry.focus_task("private:1", a.task_id)
    assert TaskRegistry(db).focused_task("private:1").task_id == a.task_id
    assert registry.focused_task("private:2").task_id == other.task_id
    registry.clear_focus("private:1")
    assert TaskRegistry(db).focused_task("private:1") is None


def test_status_word_does_not_steal_implementation_intent():
    decision = route_text("現在修 bug", default_task_router_config())
    assert decision.route == Route.DELEGATE


def test_unqualified_followup_stays_on_primary_session(monkeypatch, tmp_path: Path):
    from tg_cli_relay import telegram_bot as bot
    from test_task_isolation import FakeMessage, _drain, _env, _reset, _update

    _env(monkeypatch, tmp_path, backend="codex")
    registry = TaskRegistry(tmp_path / "sessions.sqlite3")
    focused = registry.create_task(
        parent_key="private:19", backend="codex", title="API A", status="completed"
    )
    registry.create_task(
        parent_key="private:19", backend="codex", title="API B", status="completed"
    )
    registry.focus_task("private:19", focused.task_id)
    calls = []

    async def scenario():
        _reset(bot)
        try:
            with patch.object(bot, "relay_turn", side_effect=lambda **kw: (
                calls.append(kw) or RunResult("主對話", "", 0)
            )):
                for i, text in enumerate(("剛剛那個 API 再修一下", "繼續", "進度")):
                    msg = FakeMessage(text=text, chat_id=19, message_id=100 + i)
                    await bot._on_message(_update(msg, 19), SimpleNamespace())
                    await _drain(bot)
        finally:
            _reset(bot)

    asyncio.run(scenario())
    assert [c["thread_key"] for c in calls] == ["private:19"] * 3
    assert [c["prompt"] for c in calls] == ["剛剛那個 API 再修一下", "繼續", "進度"]
    assert registry.focused_task("private:19").task_id == focused.task_id


def test_explicit_task_selection_does_not_steal_unqualified_messages(monkeypatch, tmp_path: Path):
    from tg_cli_relay import telegram_bot as bot
    from test_task_isolation import FakeMessage, _drain, _env, _reset, _update

    _env(monkeypatch, tmp_path, backend="codex")
    registry = TaskRegistry(tmp_path / "sessions.sqlite3")
    task = registry.create_task(
        parent_key="private:19", backend="codex", title="API A", status="completed"
    )
    calls = []

    async def scenario():
        _reset(bot)
        try:
            with patch.object(bot, "relay_turn", side_effect=lambda **kw: (
                calls.append(kw) or RunResult("ok", "", 0)
            )):
                command = FakeMessage(text="", chat_id=19, message_id=50)
                await bot._cmd_task(
                    _update(command, 19), SimpleNamespace(args=["use", task.task_id])
                )
                ordinary = FakeMessage(text="再修 API A 的錯誤", chat_id=19, message_id=51)
                await bot._on_message(_update(ordinary, 19), SimpleNamespace())
                await _drain(bot)
                reply = FakeMessage(text="改一下這個 Task", chat_id=19, message_id=52, reply_to=50)
                await bot._on_message(_update(reply, 19), SimpleNamespace())
                await _drain(bot)
        finally:
            _reset(bot)

    asyncio.run(scenario())
    assert calls[0]["thread_key"] == "private:19"
    # The status command's own user message ID was not bound; unknown Reply stays on main.
    assert calls[1]["thread_key"] == "private:19"
    assert "telegram_message_id" in calls[1]["prompt"]
