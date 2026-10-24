"""Regression coverage for explicit context across isolated relay tasks."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tg_cli_relay.conversation_context import ConversationContext, MAX_CHARS
from tg_cli_relay.providers.base import RunResult
from tg_cli_relay.session_store import SessionStore


@pytest.fixture
def context(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3")
    return ConversationContext(store.db_path)


def finish(context, key, request, result):
    context.complete(key, context.snapshot(key)["epoch"], request, result)


def test_task_inherits_parent_and_returns_result_without_sharing_session(context):
    parent, task = "private:1", "private:1:job:aaa"
    finish(context, parent, "哪些 WEB 搬走？", "網站甲、網站乙已搬遷")
    context.bind_task(parent, task)
    prompt = context.prompt(task, "所以這些 WEB 現況？")
    assert "網站甲、網站乙已搬遷" in prompt
    assert "Current user request:\n所以這些 WEB 現況？" in prompt
    finish(context, task, "所以這些 WEB 現況？", "已查證網站甲正常")
    parent_turns = context.snapshot(parent)["turns"]
    assert parent_turns[-1] == {
        "task": "aaa", "request": "所以這些 WEB 現況？", "result": "已查證網站甲正常"
    }
    assert "已查證網站甲正常" in context.prompt(parent, "剛剛結果呢？")
    assert context.snapshot(task)["turns"][0]["result"] == "已查證網站甲正常"


def test_existing_task_snapshots_do_not_drift_or_cross_chat(context):
    finish(context, "private:1", "原始背景", "共同背景")
    context.bind_task("private:1", "private:1:job:aaa")
    context.bind_task("private:1", "private:1:job:bbb")
    finish(context, "private:1:job:aaa", "任務 A", "只有 A 知道的結果")
    assert "只有 A 知道的結果" not in context.prompt("private:1:job:bbb", "繼續 B")
    assert "只有 A 知道的結果" in context.prompt("private:1:job:aaa", "繼續 A")
    assert context.prompt("private:2", "你好") == "你好"


def test_reset_blocks_late_parent_and_task_results(context):
    parent, task = "private:1", "private:1:job:aaa"
    old_parent_epoch = context.snapshot(parent)["epoch"]
    context.bind_task(parent, task)
    old_task_epoch = context.snapshot(task)["epoch"]
    context.reset(parent)
    context.complete(parent, old_parent_epoch, "reset 前問題", "晚到主線結果")
    context.complete(task, old_task_epoch, "舊任務", "晚到任務結果")
    assert context.snapshot(parent)["turns"] == []
    assert context.snapshot(task)["turns"][-1]["result"] == "晚到任務結果"
    context.reset(task)
    context.complete(task, old_task_epoch, "又晚到", "不得復活")
    assert context.snapshot(task)["turns"] == []


def test_concurrent_task_completions_do_not_overwrite_each_other(context):
    keys = [f"private:1:job:{index:x}" for index in range(8)]
    for key in keys:
        context.bind_task("private:1", key)
    epochs = {key: context.snapshot(key)["epoch"] for key in keys}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(context.complete, key, epochs[key], key, "完成") for key in keys]
        for future in futures:
            future.result(timeout=10)
    turns = context.snapshot("private:1")["turns"]
    assert {turn["task"] for turn in turns} == {key.rsplit(":", 1)[-1] for key in keys}


def test_context_is_bounded_and_redacts_sensitive_content(context):
    finish(context, "private:1", "token=do-not-retain", "TGR_FILE:/private/report.pdf")
    state = context.snapshot("private:1")
    serialized = json.dumps(state)
    assert "do-not-retain" not in serialized
    assert "/private/report.pdf" not in serialized
    for index in range(12):
        finish(context, "private:1", str(index) + "Q" * 3000, "A" * 5000)
    turns = context.snapshot("private:1")["turns"]
    assert 0 < len(turns) <= 8
    assert len(json.dumps(turns, ensure_ascii=False)) <= MAX_CHARS
    assert turns[-1]["request"].startswith("11")
    assert "[truncated]" in turns[-1]["result"]


def test_failed_relay_turn_is_not_saved(context, monkeypatch):
    from tg_cli_relay import relay
    store = SessionStore(context.db_path)
    monkeypatch.setattr(relay, "_relay_codex", lambda *a, **kw: RunResult("partial", "failed", 1))
    result = relay.relay_turn(thread_key="private:1", backend="codex", prompt="失敗問題",
                              store=store, workspace="/tmp")
    assert result.returncode == 1
    assert context.snapshot("private:1")["turns"] == []


def test_codex_cursor_worker_resumes_task_scoped_session_without_repeating_context(context, monkeypatch):
    from tg_cli_relay import relay
    store = SessionStore(context.db_path)
    finish(context, "private:1", "前文問題", "前文答案")
    key = "private:1:job:aaa"
    context.bind_task("private:1", key)
    store.upsert("private:other:job:bbb:codex-cursor-worker", "cursor", "other-task-session")
    provider = MagicMock()
    provider.create_session.side_effect = ["fresh-1", "fresh-2"]
    provider.run_turn.return_value = RunResult("ok", "", 0)
    monkeypatch.setenv("TGR_CODEX_DELEGATE_TO_CURSOR", "1")
    monkeypatch.setattr(relay, "CursorAgentProvider", lambda **kw: provider)
    monkeypatch.setattr(relay, "load_task_router_config", lambda: SimpleNamespace(config=None))
    monkeypatch.setattr(relay, "route_text", lambda *a: SimpleNamespace(route=relay.Route.DELEGATE))
    relay._run_codex_cursor_worker(store, key, "/tmp", "所以這些呢？")
    finish(context, key, "第一輪任務", "本任務已完成步驟")
    relay._run_codex_cursor_worker(store, key, "/tmp", "接著呢？")
    calls = provider.run_turn.call_args_list
    assert [call.kwargs["session_id"] for call in calls] == ["fresh-1", "fresh-1"]
    assert provider.create_session.call_count == 1
    assert "前文答案" in calls[0].kwargs["prompt"]
    assert "前文答案" not in calls[1].kwargs["prompt"]
    assert "本任務已完成步驟" not in calls[1].kwargs["prompt"]
    assert store.get(key + ":codex-cursor-worker", "cursor") == "fresh-1"
    assert "接著呢？" in calls[1].kwargs["prompt"]


def test_handler_handoff_reaches_real_relay_and_keeps_provider_sessions_isolated(tmp_path, monkeypatch):
    from tg_cli_relay import relay, telegram_bot as bot
    from tg_cli_relay.task_registry import TaskRegistry
    from test_task_isolation import FakeMessage, _drain, _env, _reset, _update

    _env(monkeypatch, tmp_path, backend="codex")
    monkeypatch.setenv("TGR_CODEX_DELEGATE_TO_CURSOR", "1")
    codex = MagicMock()
    codex.run_turn.side_effect = [
        RunResult("網站甲與網站乙剛搬遷", "", 0, session_id="parent-session"),
        RunResult("任務驗證完成：網站甲健康", "", 0, session_id="task-session"),
        RunResult("我已接到剛剛任務結果", "", 0, session_id="parent-session"),
    ]
    cursor = MagicMock()
    cursor.create_session.return_value = "fresh-worker-session"
    cursor.run_turn.return_value = RunResult("實際查證網站甲健康", "", 0)
    monkeypatch.setattr(relay, "CodexCliProvider", lambda **kw: codex)
    monkeypatch.setattr(relay, "CursorAgentProvider", lambda **kw: cursor)

    async def run():
        _reset(bot)
        try:
            first = FakeMessage(text="hello world", chat_id=101, message_id=100)
            await bot._on_message(_update(first, 101), SimpleNamespace())
            delegated = FakeMessage(text="請實作這些 WEB 的健康檢查", chat_id=101, message_id=101)
            await bot._cmd_task(
                _update(delegated, 101),
                SimpleNamespace(args=["new", delegated.text]),
            )
            await _drain(bot)
            last = FakeMessage(text="hello again", chat_id=101, message_id=102)
            await bot._on_message(_update(last, 101), SimpleNamespace())
        finally:
            _reset(bot)

    asyncio.run(run())
    # Relay no longer dispatches Codex -> Cursor; Task is explicit and
    # Codex receives the task along with inherited conversation reference.
    cursor.run_turn.assert_not_called()
    assert "網站甲與網站乙剛搬遷" in codex.run_turn.call_args_list[1].kwargs["prompt"]
    assert "請實作這些 WEB 的健康檢查" in codex.run_turn.call_args_list[1].kwargs["prompt"]
    calls = codex.run_turn.call_args_list
    assert len(calls) == 3
    assert [call.kwargs["session_id"] for call in calls] == [None, None, "parent-session"]
    # The resumed parent sees the unseen task completion as a delta, not full history.
    assert "new_completed_task_results" in calls[-1].kwargs["prompt"]
    assert "任務驗證完成：網站甲健康" in calls[-1].kwargs["prompt"]
    assert "任務驗證完成：網站甲健康" in ConversationContext(
        tmp_path / "sessions.sqlite3"
    ).prompt("private:101", "新 session 需要的交接")
    store = SessionStore(tmp_path / "sessions.sqlite3")
    tasks = TaskRegistry(store.db_path).list_tasks("private:101")
    assert len(tasks) == 1
    assert tasks[0].worker_key.startswith("private:101:job:")
    assert store.get(tasks[0].worker_key, "codex") == "task-session"
    assert store.get("private:101", "codex") == "parent-session"



def test_completed_task_handoff_delivered_once_to_each_resumed_parent_provider(context):
    parent, task = "private:1", "private:1:job:aaa"
    context.snapshot(parent)
    context.bind_task(parent, task)
    finish(context, task, "實作 API", "API 已修正；待整合測試")
    snapshot = context.snapshot(parent)
    first = context.prompt(parent, "接著檢查", include_history=False, consumer="codex")
    assert "API 已修正" in first
    assert "new_completed_task_results" in first
    context.acknowledge(parent, snapshot["epoch"], "codex", snapshot["next_handoff_id"])
    assert context.prompt(parent, "再問一次", include_history=False, consumer="codex") == "再問一次"
    assert "API 已修正" in context.prompt(
        parent, "換個 backend", include_history=False, consumer="cursor"
    )
    finish(context, task, "跑測試", "測試通過")
    next_delta = context.prompt(parent, "現在如何", include_history=False, consumer="codex")
    assert "測試通過" in next_delta
    assert "API 已修正" not in next_delta


def test_reset_rejects_late_handoff_ack(context):
    parent, task = "private:7", "private:7:job:xyz"
    old = context.snapshot(parent)
    context.bind_task(parent, task)
    finish(context, task, "問題", "結果")
    context.reset(parent)
    context.acknowledge(parent, old["epoch"], "codex", 999)
    new = context.snapshot(parent)
    assert new["epoch"] != old["epoch"]
    assert "delivered" not in new
