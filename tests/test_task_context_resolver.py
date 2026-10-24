"""No-MCP semantic task routing through the current primary CLI."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tg_cli_relay.providers.base import RunResult
from tg_cli_relay.task_context_resolver import (
    might_refer_to_task,
    parse_resolution,
    resolution_prompt,
)
from tg_cli_relay.task_registry import TaskRegistry
from test_task_isolation import FakeMessage, _drain, _env, _reset, _update


def test_task_context_is_compact_and_exact(tmp_path: Path):
    registry = TaskRegistry(tmp_path / "sessions.sqlite3")
    a = registry.create_task(parent_key="private:1", backend="codex", title="分點補檔修復")
    registry.create_task(parent_key="private:2", backend="codex", title="別人的密碼")
    tasks = registry.list_tasks("private:1")
    text = resolution_prompt("補檔那個不要重跑", tasks)
    assert a.task_id in text
    assert "別人的密碼" not in text
    assert "補檔那個不要重跑" in text
    assert might_refer_to_task("補檔不要重跑", tasks)
    assert not might_refer_to_task("今天天氣如何", tasks)


def test_model_selection_is_fail_closed(tmp_path: Path):
    registry = TaskRegistry(tmp_path / "sessions.sqlite3")
    task = registry.create_task(parent_key="private:1", backend="codex", title="補檔")
    available = registry.list_tasks("private:1")
    assert parse_resolution(
        json.dumps({"action": "task", "task_id": task.task_id}), available
    ).task_id == task.task_id
    for response in (
        "some prose", '{"action":"task","task_id":"not-found"}',
        '{"action":"task","task_id":"not-found","reason":"trust me"}',
        '{"action":"new","task_id":"' + task.task_id + '"}',
        '{"action":"task","task_id":null}',
    ):
        assert parse_resolution(response, available).action == "ask"
    assert parse_resolution('{"action":"new"}', available).action == "new"


def test_primary_agent_selects_task_even_if_focus_points_elsewhere(monkeypatch, tmp_path: Path):
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, "codex")
    registry = TaskRegistry(tmp_path / "sessions.sqlite3")
    target = registry.create_task(parent_key="private:101", backend="codex", title="分點補檔修復", status="completed")
    other = registry.create_task(parent_key="private:101", backend="codex", title="三箭報告", status="completed")
    assert registry.focused_task("private:101").task_id == other.task_id
    calls = []
    msg = FakeMessage(text="補檔那邊不要全部重跑", chat_id=101, message_id=321)

    def relay(**kwargs):
        calls.append(kwargs)
        if kwargs.get("routing_only"):
            assert target.task_id in kwargs["prompt"]
            assert other.task_id in kwargs["prompt"]
            return RunResult(json.dumps({"action": "task", "task_id": target.task_id}), "", 0)
        return RunResult("處理完成", "", 0)

    async def scenario():
        _reset(bot)
        try:
            with patch.object(bot, "relay_turn", side_effect=relay):
                await bot._on_message(_update(msg, 101), SimpleNamespace())
                await _drain(bot)
        finally:
            _reset(bot)

    asyncio.run(scenario())
    assert len(calls) == 2
    assert calls[0]["thread_key"] == "private:101" and calls[0]["routing_only"]
    assert calls[1]["thread_key"] == target.worker_key
    assert calls[1]["prompt"] == "補檔那邊不要全部重跑"
    assert registry.get_task_for_message("private:101", 321).task_id == target.task_id


def test_primary_agent_says_new_and_preserves_original_text(monkeypatch, tmp_path: Path):
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, "codex")
    registry = TaskRegistry(tmp_path / "sessions.sqlite3")
    registry.create_task(parent_key="private:13", backend="codex", title="三箭報告", status="completed")
    msg = FakeMessage(text="那個報告是哪天發表的？", chat_id=13, message_id=9)
    calls = []

    def relay(**kwargs):
        calls.append(kwargs)
        if kwargs.get("routing_only"):
            return RunResult('{"action":"new"}', "", 0)
        return RunResult("主對話回答", "", 0)

    async def scenario():
        _reset(bot)
        try:
            with patch.object(bot, "relay_turn", side_effect=relay):
                await bot._on_message(_update(msg, 13), SimpleNamespace())
                await _drain(bot)
        finally:
            _reset(bot)

    asyncio.run(scenario())
    assert len(calls) == 2
    assert calls[0]["routing_only"]
    assert calls[1]["thread_key"] == "private:13"
    assert calls[1]["prompt"] == "那個報告是哪天發表的？"
    assert registry.get_parent_message_backend("private:13", 9) == "codex"


def test_invalid_cross_chat_choice_never_dispatches(monkeypatch, tmp_path: Path):
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, "claude")
    registry = TaskRegistry(tmp_path / "sessions.sqlite3")
    a = registry.create_task(parent_key="private:13", backend="claude", title="三箭報告", status="completed")
    b = registry.create_task(parent_key="private:14", backend="claude", title="不屬於這個對話", status="completed")
    msg = FakeMessage(text="那個報告繼續處理", chat_id=13, message_id=12)
    calls = []

    def relay(**kwargs):
        calls.append(kwargs)
        return RunResult(json.dumps({"action": "task", "task_id": b.task_id}), "", 0)

    async def scenario():
        _reset(bot)
        try:
            with patch.object(bot, "relay_turn", side_effect=relay):
                await bot._on_message(_update(msg, 13), SimpleNamespace())
                await _drain(bot)
        finally:
            _reset(bot)

    asyncio.run(scenario())
    assert len(calls) == 1 and calls[0]["routing_only"]
    assert a.task_id in msg.sent[-1].text
    assert b.task_id not in msg.sent[-1].text
    assert registry.get_task_for_message("private:13", 12) is None


def test_unrelated_question_does_not_call_classifier(monkeypatch, tmp_path: Path):
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, "codex")
    TaskRegistry(tmp_path / "sessions.sqlite3").create_task(
        parent_key="private:13", backend="codex", title="分點補檔修復", status="completed"
    )
    msg = FakeMessage(text="今天要吃什麼？", chat_id=13, message_id=7)
    calls = []

    async def scenario():
        _reset(bot)
        try:
            with patch.object(bot, "relay_turn", side_effect=lambda **kw: (
                calls.append(kw) or RunResult("主對話回覆", "", 0)
            )):
                await bot._on_message(_update(msg, 13), SimpleNamespace())
                await _drain(bot)
        finally:
            _reset(bot)

    asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0]["prompt"] == "今天要吃什麼？"
    assert not calls[0].get("routing_only", False)


def test_ambiguous_status_uses_only_status_not_worker(monkeypatch, tmp_path: Path):
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, "codex")
    registry = TaskRegistry(tmp_path / "sessions.sqlite3")
    a = registry.create_task(parent_key="private:13", backend="codex", title="補檔", status="running")
    registry.create_task(parent_key="private:13", backend="codex", title="報告", status="running")
    registry.clear_focus("private:13")
    msg = FakeMessage(text="進度", chat_id=13, message_id=42)
    calls = []

    async def scenario():
        _reset(bot)
        try:
            with patch.object(bot, "relay_turn", side_effect=lambda **kw: (
                calls.append(kw) or RunResult(
                    json.dumps({"action":"task","task_id":a.task_id}), "", 0
                )
            )):
                await bot._on_message(_update(msg, 13), SimpleNamespace())
                await _drain(bot)
        finally:
            _reset(bot)

    asyncio.run(scenario())
    assert len(calls) == 1 and calls[0]["routing_only"]
    assert a.task_id in msg.sent[-1].text
    assert registry.get_task_for_message("private:13", 42).task_id == a.task_id
