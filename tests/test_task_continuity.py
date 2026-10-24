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


def test_telegram_natural_followup_uses_focused_task_without_reply(monkeypatch, tmp_path: Path):
    from tg_cli_relay import telegram_bot as bot
    from test_task_isolation import FakeMessage, _drain, _env, _reset, _update

    _env(monkeypatch, tmp_path, backend="codex")
    registry = TaskRegistry(tmp_path / "sessions.sqlite3")
    first = registry.create_task(parent_key="private:19", backend="codex", title="API A", status="completed")
    second = registry.create_task(parent_key="private:19", backend="codex", title="API B", status="completed")
    registry.focus_task("private:19", first.task_id)
    calls = []

    def relay(**kw):
        calls.append((kw["thread_key"], kw["prompt"], kw.get("routing_only", False)))
        if kw.get("routing_only"):
            return RunResult(stdout='{"action":"task","task_id":"' + first.task_id + '"}', stderr="", returncode=0)
        return RunResult(stdout="修正完成", stderr="", returncode=0)

    async def scenario():
        _reset(bot)
        try:
            with patch.object(bot, "relay_turn", side_effect=relay):
                msg = FakeMessage(text="剛剛那個 API 再修一下", chat_id=19, message_id=100)
                await bot._on_message(_update(msg, 19), SimpleNamespace())
                await _drain(bot)
                assert len(calls) == 2
                assert calls[0][0] == "private:19" and calls[0][2]
                assert calls[1] == (first.worker_key, "剛剛那個 API 再修一下", False)
                assert registry.focused_task("private:19").task_id == first.task_id
                status = FakeMessage(text="進度", chat_id=19, message_id=101)
                await bot._on_message(_update(status, 19), SimpleNamespace())
                assert first.task_id in status.sent[-1].text
                assert len(calls) == 2
        finally:
            _reset(bot)

    asyncio.run(scenario())


def test_explicit_task_switch_then_natural_followup(monkeypatch, tmp_path: Path):
    from tg_cli_relay import telegram_bot as bot
    from test_task_isolation import FakeMessage, _drain, _env, _reset, _update

    _env(monkeypatch, tmp_path, backend="codex")
    registry = TaskRegistry(tmp_path / "sessions.sqlite3")
    first = registry.create_task(parent_key="private:19", backend="codex", title="API A", status="completed")
    second = registry.create_task(parent_key="private:19", backend="codex", title="API B", status="completed")
    calls = []

    async def scenario():
        _reset(bot)
        try:
            with patch.object(
                bot, "relay_turn",
                side_effect=lambda **kw: (
                    calls.append(kw["thread_key"]) or RunResult(
                        '{"action":"task","task_id":"' + first.task_id + '"}'
                        if kw.get("routing_only") else "ok", "", 0
                    )
                ),
            ):
                command = FakeMessage(text="", chat_id=19, message_id=50)
                await bot._cmd_task(
                    _update(command, 19), SimpleNamespace(args=["use", first.task_id])
                )
                followup = FakeMessage(text="再修 API A 的錯誤", chat_id=19, message_id=51)
                await bot._on_message(_update(followup, 19), SimpleNamespace())
                await _drain(bot)
        finally:
            _reset(bot)

    asyncio.run(scenario())
    assert calls == ["private:19", first.worker_key]
    assert TaskRegistry(tmp_path / "sessions.sqlite3").focused_task("private:19").task_id == first.task_id
    assert first.task_id != second.task_id
