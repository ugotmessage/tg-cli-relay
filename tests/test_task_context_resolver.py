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


def test_relay_does_not_invoke_semantic_classifier(monkeypatch, tmp_path: Path):
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, "codex")
    reg = TaskRegistry(tmp_path / "sessions.sqlite3")
    reg.create_task(parent_key="private:101", backend="codex", title="分點補檔修復")
    reg.create_task(parent_key="private:101", backend="codex", title="三箭報告")
    calls = []

    async def scenario():
        _reset(bot)
        try:
            with patch.object(bot, "relay_turn", side_effect=lambda **kw: (
                calls.append(kw) or RunResult("主 Agent 自行判斷", "", 0)
            )):
                for i, text in enumerate((
                    "補檔那邊不要全部重跑", "那個報告是哪天發表的？",
                    "進度", "現在進度如何", "繼續", "今天要吃什麼？",
                )):
                    msg = FakeMessage(text=text, chat_id=101, message_id=321 + i)
                    await bot._on_message(_update(msg, 101), SimpleNamespace())
                    await _drain(bot)
        finally:
            _reset(bot)

    asyncio.run(scenario())
    assert len(calls) == 6
    assert all(c["thread_key"] == "private:101" for c in calls)
    assert all(not c.get("routing_only") for c in calls)
    assert [c["prompt"] for c in calls] == [
        "補檔那邊不要全部重跑", "那個報告是哪天發表的？",
        "進度", "現在進度如何", "繼續", "今天要吃什麼？",
    ]
    assert reg.list_tasks("private:101")
    assert all(reg.get_task_for_message("private:101", 321 + i) is None for i in range(6))


def test_explicit_cross_chat_reply_cannot_select_other_task(monkeypatch, tmp_path: Path):
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, "claude")
    reg = TaskRegistry(tmp_path / "sessions.sqlite3")
    other = reg.create_task(parent_key="private:14", backend="claude", title="別人的工作")
    reg.bind_message(
        parent_key="private:14", message_id=40, task_id=other.task_id,
        backend="claude", role="source",
    )
    calls = []

    async def scenario():
        _reset(bot)
        try:
            with patch.object(bot, "relay_turn", side_effect=lambda **kw: (
                calls.append(kw) or RunResult("ok", "", 0)
            )):
                msg = FakeMessage(text="要繼續這個", chat_id=13, message_id=41, reply_to=40)
                await bot._on_message(_update(msg, 13), SimpleNamespace())
                await _drain(bot)
        finally:
            _reset(bot)

    asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0]["thread_key"] == "private:13"
    assert "telegram_message_id" in calls[0]["prompt"]
