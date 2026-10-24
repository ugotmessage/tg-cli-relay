"""Durable queue crash/restart semantics. Never silently re-execute an uncertain turn."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tg_cli_relay.durable_queue import DurableQueue
from tg_cli_relay.providers.base import RunResult
from test_task_isolation import FakeMessage, _env, _reset, _update


def _add(db: Path, *, mid=1, kind="parent", prompt="原始指令", thread="private:7"):
    return DurableQueue(db).add(
        thread_key=thread, backend="codex", prompt=prompt, kind=kind,
        chat_id=7, message_id=mid, topic_id=None,
    )


def test_durable_queue_idempotence_and_uncertain_state(tmp_path):
    db = tmp_path / "sessions.sqlite3"
    first = _add(db, mid=10)
    assert _add(db, mid=10) == first
    waiting = _add(db, mid=11)
    store = DurableQueue(db)
    assert store.start(first)
    assert not store.start(first)
    assert store.mark_interrupted_uncertain() == 1
    assert [r.id for r in store.load_queued()] == [waiting]
    assert store.review_threads() == {"private:7"}
    assert {r.status for r in store.open_items("private:7")} == {"queued", "needs_review"}
    assert store.reviewed("private:7") == 1
    assert store.review_threads() == set()
    assert store.start(waiting)
    store.finish(waiting)
    assert store.load_queued() == []


def test_priority_recovery_is_restored_before_normal_chat(tmp_path):
    db = tmp_path / "sessions.sqlite3"
    _add(db, mid=12, prompt="排隊訊息")
    _add(db, mid=11, kind="timeout_recovery", prompt="先恢復超時工作")
    assert [r.prompt for r in DurableQueue(db).load_queued()] == [
        "先恢復超時工作", "排隊訊息",
    ]


def test_restart_autoreplays_only_unstarted_turns(monkeypatch, tmp_path):
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, backend="codex")
    db = tmp_path / "sessions.sqlite3"
    _add(db, mid=1, prompt="訊息 A")
    _add(db, mid=2, prompt="訊息 B")
    bot_api = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=771)),
        send_chat_action=AsyncMock(),
        send_document=AsyncMock(),
    )
    calls = []

    async def scenario():
        _reset(bot)
        bot._REVIEW_REQUIRED_THREADS.clear()
        try:
            with patch.object(bot, "relay_turn", side_effect=lambda **kw: (
                calls.append(kw["prompt"]) or RunResult("done", "", 0)
            )):
                await bot._restore_durable_queue(bot_api)
                while bot._BACKGROUND_TASKS:
                    await asyncio.gather(*tuple(bot._BACKGROUND_TASKS), return_exceptions=True)
        finally:
            _reset(bot)
            bot._REVIEW_REQUIRED_THREADS.clear()

    asyncio.run(scenario())
    assert calls == ["訊息 A", "訊息 B"]
    assert DurableQueue(db).open_items("private:7") == []
    assert bot_api.send_message.await_count >= 2


def test_uncertain_turn_pauses_backlog_until_queue_resume(monkeypatch, tmp_path):
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, backend="codex")
    db = tmp_path / "sessions.sqlite3"
    previous = _add(db, mid=50, prompt="不確定已經寫入資料庫")
    assert DurableQueue(db).start(previous)
    _add(db, mid=51, prompt="等待中的下一則訊息")
    bot_api = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=772)),
        send_chat_action=AsyncMock(),
        send_document=AsyncMock(),
    )
    calls = []

    async def scenario():
        _reset(bot)
        bot._REVIEW_REQUIRED_THREADS.clear()
        try:
            with patch.object(bot, "relay_turn", side_effect=lambda **kw: (
                calls.append(kw["prompt"]) or RunResult("ok", "", 0)
            )):
                await bot._restore_durable_queue(bot_api)
                await asyncio.sleep(0)
                assert calls == []
                assert "private:7" in bot._REVIEW_REQUIRED_THREADS
                listing = FakeMessage(text="/queue", chat_id=7, message_id=61)
                await bot._cmd_queue(_update(listing, 7), SimpleNamespace(args=[]))
                assert "結果不明" in listing.sent[-1].text
                resume = FakeMessage(text="/queue resume", chat_id=7, message_id=62)
                await bot._cmd_queue(_update(resume, 7), SimpleNamespace(args=["resume"]))
                while bot._BACKGROUND_TASKS:
                    await asyncio.gather(*tuple(bot._BACKGROUND_TASKS), return_exceptions=True)
        finally:
            _reset(bot)
            bot._REVIEW_REQUIRED_THREADS.clear()

    asyncio.run(scenario())
    assert calls == ["等待中的下一則訊息"]
    assert any(
        "狀態不明" in c.kwargs.get("text", "")
        for c in bot_api.send_message.await_args_list
    )
    assert DurableQueue(db).review_threads() == set()
    assert DurableQueue(db).open_items("private:7") == []


def test_duplicate_telegram_update_does_not_reexecute(monkeypatch, tmp_path):
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, backend="cursor")
    calls = []

    async def scenario():
        _reset(bot)
        bot._REVIEW_REQUIRED_THREADS.clear()
        try:
            with patch.object(bot, "relay_turn", side_effect=lambda **kw: (
                calls.append(kw["prompt"]) or RunResult("ok", "", 0)
            )):
                for _ in range(2):
                    msg = FakeMessage(text="請不要重複執行", chat_id=7, message_id=1)
                    await bot._on_message(_update(msg, 7), SimpleNamespace())
        finally:
            _reset(bot)
            bot._REVIEW_REQUIRED_THREADS.clear()

    asyncio.run(scenario())
    assert calls == ["請不要重複執行"]


def test_cancelled_inflight_turn_is_never_marked_done(monkeypatch, tmp_path):
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, backend="codex")
    started = asyncio.Event()

    async def never_finishes(**kw):
        started.set()
        await asyncio.Future()

    async def scenario():
        _reset(bot)
        bot._REVIEW_REQUIRED_THREADS.clear()
        try:
            msg = FakeMessage(text="可能有副作用的操作", chat_id=7, message_id=900)
            turn = bot.QueuedTurn(
                msg=msg, key="private:7", backend="codex", prompt=msg.text,
                store_path=tmp_path / "sessions.sqlite3",
                attachment_config=bot._attachment_config(), route=bot.Route.DIRECT,
            )
            with patch.object(bot, "_execute_relay_turn", side_effect=never_finishes):
                running = asyncio.create_task(bot._run_direct_turn(turn))
                await started.wait()
                running.cancel()
                try:
                    await running
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("cancelled task did not propagate")
            pending = DurableQueue(tmp_path / "sessions.sqlite3").open_items("private:7")
            assert len(pending) == 1
            assert pending[0].status == "needs_review"
            assert "private:7" in bot._REVIEW_REQUIRED_THREADS
        finally:
            _reset(bot)
            bot._REVIEW_REQUIRED_THREADS.clear()

    asyncio.run(scenario())


def test_terminal_history_can_be_pruned_without_dropping_waiting_turns(tmp_path):
    db = tmp_path / "sessions.sqlite3"
    store = DurableQueue(db)
    done = _add(db, mid=77)
    waiting = _add(db, mid=78)
    store.start(done)
    store.finish(done)
    import sqlite3
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE relay_pending_turns SET updated_at=datetime('now','-45 days') WHERE id=?",
            (done,),
        )
    assert store.prune_terminal(retention_days=30) == 1
    assert [r.id for r in store.load_queued()] == [waiting]
