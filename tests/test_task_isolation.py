"""Handler→relay boundary tests for Telegram task isolation."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tg_cli_relay.providers.base import RunResult
from tg_cli_relay.session_store import SessionStore
from tg_cli_relay.task_registry import TaskRegistry, make_task_title
from tg_cli_relay.thread_key import TelegramIds, telegram_thread_key


_SENT_ID = 50000


def _next_sent_id() -> int:
    global _SENT_ID
    _SENT_ID += 1
    return _SENT_ID


class FakeMessage:
    def __init__(
        self,
        *,
        text: str,
        chat_id: int,
        message_id: int,
        reply_to: int | None = None,
        thread_id: int | None = None,
    ) -> None:
        self.text = text
        self.caption = None
        self.document = None
        self.photo = None
        self.audio = None
        self.voice = None
        self.video = None
        self.chat_id = chat_id
        self.message_id = message_id
        self.message_thread_id = thread_id
        self.reply_to_message = (
            SimpleNamespace(message_id=reply_to) if reply_to is not None else None
        )
        self.chat = SimpleNamespace(send_action=AsyncMock())
        self.sent: list[SimpleNamespace] = []
        self.reply_text = AsyncMock(side_effect=self._reply)
        self.reply_document = AsyncMock(side_effect=self._reply_doc)

    def _reply(self, text: str) -> SimpleNamespace:
        sent = SimpleNamespace(message_id=_next_sent_id(), text=text)
        self.sent.append(sent)
        return sent

    def _reply_doc(self, **kwargs) -> SimpleNamespace:
        sent = SimpleNamespace(message_id=_next_sent_id(), filename=kwargs.get("filename"))
        self.sent.append(sent)
        return sent


def _update(msg: FakeMessage, chat_id: int, chat_type: str = "private"):
    chat = SimpleNamespace(id=chat_id, type=chat_type, send_message=AsyncMock())
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=chat,
        message=msg,
    )


def _env(monkeypatch, tmp_path: Path, backend: str = "cursor") -> None:
    monkeypatch.setenv("TGR_SESSION_DB", str(tmp_path / "sessions.sqlite3"))
    monkeypatch.setenv("TGR_DEFAULT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("TGR_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("TGR_ROUTER_CONFIG", str(tmp_path / "missing-router.toml"))
    monkeypatch.setenv("TGR_BACKEND", backend)
    monkeypatch.setenv("TGR_ENABLED_BACKENDS", "cursor,codex,claude,opencode")
    monkeypatch.delenv("TGR_ALLOWED_TELEGRAM_USER_IDS", raising=False)


def _reset(bot) -> None:
    bot._ACTIVE_JOBS.clear()
    bot._RECENT_JOBS.clear()
    bot._RUNNING_TASKS.clear()
    bot._PENDING_TURNS.clear()
    bot._PENDING_TASK_TURNS.clear()
    bot._QUEUE_DRAIN_TASKS.clear()
    bot._TASK_DRAIN_TASKS.clear()
    bot._ACTIVE_RELAY_THREADS.clear()
    bot._BACKGROUND_TASKS.clear()


async def _drain(bot) -> None:
    while bot._BACKGROUND_TASKS:
        await asyncio.gather(*tuple(bot._BACKGROUND_TASKS), return_exceptions=True)


def _task_id_from_sent(msg: FakeMessage) -> str:
    for sent in msg.sent:
        text = getattr(sent, "text", "") or ""
        marker = "[task "
        if marker in text:
            start = text.index(marker) + len(marker)
            return text[start:].split("]", 1)[0].strip()
    raise AssertionError(f"no task banner in {msg.sent!r}")


def test_additive_migration_leaves_sessions_and_preferences(tmp_path: Path) -> None:
    db = tmp_path / "sessions.sqlite3"
    store = SessionStore(db)
    store.upsert("private:1", "cursor", "sess-legacy", workspace="/tmp/ws")
    store.set_pref("private:1", "backend", "cursor")
    store.set_pref("private:1", "model:cursor", "auto")

    registry = TaskRegistry(db)
    registry.create_task(parent_key="private:1", backend="cursor", title="new")

    conn = sqlite3.connect(db)
    try:
        sessions = conn.execute("SELECT thread_key, backend, session_id FROM sessions").fetchall()
        prefs = conn.execute("SELECT thread_key, key, value FROM preferences ORDER BY key").fetchall()
    finally:
        conn.close()
    assert sessions == [("private:1", "cursor", "sess-legacy")]
    assert prefs == [
        ("private:1", "backend", "cursor"),
        ("private:1", "model:cursor", "auto"),
    ]
    assert store.get("private:1", "cursor") == "sess-legacy"
    assert store.get_pref("private:1", "backend") == "cursor"


def test_handler_task_isolation_routing(monkeypatch, tmp_path: Path) -> None:
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, backend="codex")
    calls: list[dict] = []
    hold_b = threading.Event()
    b_started = threading.Event()
    occupancy: dict[str, dict[str, int]] = {}
    occupancy_lock = threading.Lock()

    def relay(**kwargs):
        calls.append(
            {
                "thread_key": kwargs["thread_key"],
                "prompt": kwargs["prompt"],
                "backend": kwargs["backend"],
                "has_progress": kwargs.get("on_progress") is not None,
            }
        )
        progress = kwargs.get("on_progress")
        if progress is not None and "任務A" in kwargs["prompt"] and "follow" not in kwargs["prompt"]:
            progress("A 進度區塊")
        key = kwargs["thread_key"]
        with occupancy_lock:
            slot = occupancy.setdefault(key, {"current": 0, "max": 0})
            slot["current"] += 1
            slot["max"] = max(slot["max"], slot["current"])
        try:
            if "任務B" in kwargs["prompt"] and "follow" not in kwargs["prompt"]:
                b_started.set()
                hold_b.wait(timeout=5)
            if "serial-slow" in kwargs["prompt"]:
                time.sleep(0.2)
            return RunResult(stdout=f"done:{kwargs['prompt']}", stderr="", returncode=0)
        finally:
            with occupancy_lock:
                occupancy[key]["current"] -= 1

    async def _run():
        _reset(bot)
        with patch.object(bot, "relay_turn", side_effect=relay):
            store_path = tmp_path / "sessions.sqlite3"
            chat_a = 101

            ordinary = FakeMessage(text="hello world", chat_id=chat_a, message_id=1)
            await bot._on_message(_update(ordinary, chat_a), SimpleNamespace())
            assert calls[0]["thread_key"] == "private:101"
            assert calls[0]["prompt"] == "hello world"

            msg_a = FakeMessage(text="請實作任務A獨立功能", chat_id=chat_a, message_id=2)
            await bot._on_message(_update(msg_a, chat_a), SimpleNamespace())
            await _drain(bot)
            a_key = calls[1]["thread_key"]
            a_id = _task_id_from_sent(msg_a)
            assert calls[1]["backend"] == "codex"
            assert a_key == f"private:101:job:{a_id}"
            assert a_key != "private:101"
            assert any("[task " in (s.text or "") and a_id in (s.text or "") for s in msg_a.sent)
            sent_texts = [s.text for s in msg_a.sent]
            final_a = next(
                s.message_id
                for s in msg_a.sent
                if s.text and "done:請實作任務A獨立功能" in s.text
            )
            progress_a = next(
                (
                    s.message_id
                    for s in msg_a.sent
                    if s.text and "A 進度區塊" in s.text
                ),
                None,
            )
            assert progress_a is not None, sent_texts

            msg_b = FakeMessage(text="請實作任務B獨立功能", chat_id=chat_a, message_id=3)
            await bot._on_message(_update(msg_b, chat_a), SimpleNamespace())
            assert await asyncio.to_thread(b_started.wait, 2)
            b_key = next(c["thread_key"] for c in calls if "任務B" in c["prompt"])
            b_id = _task_id_from_sent(msg_b)
            assert b_key == f"private:101:job:{b_id}"
            assert a_key != b_key

            reply_a = FakeMessage(
                text="follow A 補測試",
                chat_id=chat_a,
                message_id=4,
                reply_to=msg_a.message_id,
            )
            await bot._on_message(_update(reply_a, chat_a), SimpleNamespace())
            await asyncio.sleep(0.05)
            follow_calls = [c for c in calls if c["prompt"] == "follow A 補測試"]
            assert follow_calls
            assert follow_calls[0]["thread_key"] == a_key
            assert follow_calls[0]["backend"] == "codex"

            hold_b.set()
            await _drain(bot)

            reply_progress = FakeMessage(
                text="follow via progress",
                chat_id=chat_a,
                message_id=5,
                reply_to=progress_a,
            )
            await bot._on_message(_update(reply_progress, chat_a), SimpleNamespace())
            await _drain(bot)
            assert calls[-1]["thread_key"] == a_key
            assert calls[-1]["prompt"] == "follow via progress"

            reply_final = FakeMessage(
                text="follow via final",
                chat_id=chat_a,
                message_id=6,
                reply_to=final_a,
            )
            await bot._on_message(_update(reply_final, chat_a), SimpleNamespace())
            await _drain(bot)
            assert calls[-1]["thread_key"] == a_key
            assert calls[-1]["prompt"] == "follow via final"

            provider_before = len(calls)
            ambiguous = FakeMessage(text="現在進度如何", chat_id=chat_a, message_id=7)
            await bot._on_message(_update(ambiguous, chat_a), SimpleNamespace())
            assert len(calls) == provider_before
            choice = ambiguous.sent[-1].text
            assert a_id in choice and b_id in choice
            assert "/task" in choice

            other_chat = FakeMessage(
                text="follow stolen",
                chat_id=202,
                message_id=8,
                reply_to=msg_a.message_id,
            )
            await bot._on_message(_update(other_chat, 202), SimpleNamespace())
            stolen = [c for c in calls if c["prompt"] == "follow stolen"]
            assert all(a_key not in c["thread_key"] and c["thread_key"] != a_key for c in stolen)

            ctx = SimpleNamespace(args=[a_id])
            foreign_msg = FakeMessage(text="/task", chat_id=202, message_id=9)
            await bot._cmd_task(_update(foreign_msg, 202), ctx)
            assert "不屬於這個對話" in foreign_msg.sent[-1].text

            unknown_msg = FakeMessage(text="/task", chat_id=chat_a, message_id=10)
            await bot._cmd_task(_update(unknown_msg, chat_a), SimpleNamespace(args=["deadbeef"]))
            assert "找不到任務 deadbeef" in unknown_msg.sent[-1].text

            topic1 = telegram_thread_key(
                chat_type="supergroup", ids=TelegramIds(9, 11)
            )
            topic_msg = FakeMessage(
                text="請實作 topic 任務",
                chat_id=9,
                message_id=11,
                thread_id=11,
            )
            await bot._on_message(
                _update(topic_msg, 9, chat_type="supergroup"),
                SimpleNamespace(),
            )
            await _drain(bot)
            topic_key = next(c["thread_key"] for c in calls if c["prompt"] == "請實作 topic 任務")
            assert topic_key.startswith("topic:9:11:job:")
            topic_id = _task_id_from_sent(topic_msg)
            other_topic = FakeMessage(
                text="follow topic",
                chat_id=9,
                message_id=12,
                thread_id=12,
                reply_to=topic_msg.message_id,
            )
            await bot._on_message(
                _update(other_topic, 9, chat_type="supergroup"),
                SimpleNamespace(),
            )
            assert all(
                c["thread_key"] != topic_key for c in calls if c["prompt"] == "follow topic"
            )
            topic_cmd = FakeMessage(text="/task", chat_id=9, message_id=14, thread_id=12)
            await bot._cmd_task(
                _update(topic_cmd, 9, chat_type="supergroup"),
                SimpleNamespace(args=[topic_id]),
            )
            assert "不屬於這個對話" in topic_cmd.sent[-1].text
            assert topic1.startswith("topic:9:11")

            store = SessionStore(store_path)
            store.set_pref("private:101", "backend", "cursor")
            cross_backend = FakeMessage(
                text="follow after provider switch",
                chat_id=chat_a,
                message_id=15,
                reply_to=msg_a.message_id,
            )
            await bot._on_message(_update(cross_backend, chat_a), SimpleNamespace())
            await _drain(bot)
            switched = [c for c in calls if c["prompt"] == "follow after provider switch"]
            assert switched[-1]["thread_key"] == a_key
            assert switched[-1]["backend"] == "codex"

            registry = TaskRegistry(store_path)
            assert registry.get_task(a_id).status == "completed"
            assert registry.get_task(b_id).status == "completed"

            _reset(bot)
            reopened = TaskRegistry(store_path)
            assert reopened.get_task_for_message("private:101", msg_a.message_id).task_id == a_id
            reopen_reply = FakeMessage(
                text="follow after reopen",
                chat_id=chat_a,
                message_id=16,
                reply_to=msg_a.message_id,
            )
            await bot._on_message(_update(reopen_reply, chat_a), SimpleNamespace())
            await _drain(bot)
            assert calls[-1]["thread_key"] == a_key
            assert calls[-1]["prompt"] == "follow after reopen"

            hold_c = threading.Event()
            c_started = threading.Event()
            original_relay = relay

            def relay_with_c(**kwargs):
                if "任務C" in kwargs["prompt"]:
                    c_started.set()
                    hold_c.wait(timeout=5)
                return original_relay(**kwargs)

            bot.relay_turn.side_effect = relay_with_c
            msg_c = FakeMessage(text="請實作任務C獨立功能", chat_id=chat_a, message_id=17)
            await bot._on_message(_update(msg_c, chat_a), SimpleNamespace())
            assert await asyncio.to_thread(c_started.wait, 2)
            c_id = _task_id_from_sent(msg_c)
            c_key = f"private:101:job:{c_id}"
            f1 = FakeMessage(
                text="serial-slow follow 1",
                chat_id=chat_a,
                message_id=18,
                reply_to=msg_c.message_id,
            )
            f2 = FakeMessage(
                text="serial-slow follow 2",
                chat_id=chat_a,
                message_id=19,
                reply_to=msg_c.message_id,
            )
            await bot._on_message(_update(f1, chat_a), SimpleNamespace())
            await bot._on_message(_update(f2, chat_a), SimpleNamespace())
            queued_text = f2.sent[-1].text
            assert c_id in queued_text
            hold_c.set()
            await _drain(bot)
            serial = [c for c in calls if c["prompt"].startswith("serial-slow follow")]
            assert [c["thread_key"] for c in serial] == [c_key, c_key]
            assert occupancy[c_key]["max"] == 1
            assert all(slot["max"] == 1 for slot in occupancy.values())

            a_status = reopened.get_task(a_id).status
            reopened.update_status(c_id, "completed")
            assert reopened.get_task(a_id).status == a_status
            assert reopened.get_task(b_id).status == "completed"

            small = FakeMessage(text="/task new", chat_id=chat_a, message_id=20)
            await bot._cmd_task(
                _update(small, chat_a),
                SimpleNamespace(args=["new", "查一下版本"]),
            )
            await _drain(bot)
            small_call = next(c for c in calls if c["prompt"] == "查一下版本")
            assert ":job:" in small_call["thread_key"]
            assert small_call["thread_key"] != "private:101"

            unbound = FakeMessage(
                text="這則沒綁定",
                chat_id=chat_a,
                message_id=21,
                reply_to=999999,
            )
            before = len(calls)
            await bot._on_message(_update(unbound, chat_a), SimpleNamespace())
            assert len(calls) == before
            assert "沒有綁定任務" in unbound.sent[-1].text

        _reset(bot)

    asyncio.run(_run())


def test_failed_send_and_provider_lifecycle(monkeypatch, tmp_path: Path) -> None:
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path)
    store_path = tmp_path / "sessions.sqlite3"

    async def _run():
        _reset(bot)
        msg = FakeMessage(text="請實作失敗 ACK", chat_id=11, message_id=1)
        with patch.object(
            bot,
            "_reply_text_with_retry",
            new=AsyncMock(side_effect=RuntimeError("ack failed")),
        ), patch.object(bot, "relay_turn") as relay:
            with pytest.raises(RuntimeError, match="ack failed"):
                await bot._on_message(_update(msg, 11), SimpleNamespace())
            relay.assert_not_called()
        registry = TaskRegistry(store_path)
        tasks = registry.list_tasks("private:11")
        assert len(tasks) == 1
        assert tasks[0].status == "failed"
        assert registry.get_task_for_message("private:11", 1) is not None

        raised = FakeMessage(text="請實作 provider boom", chat_id=12, message_id=2)

        def boom(**kwargs):
            raise RuntimeError("provider down")

        with patch.object(bot, "relay_turn", side_effect=boom):
            await bot._on_message(_update(raised, 12), SimpleNamespace())
            await _drain(bot)
        failed = TaskRegistry(store_path).list_tasks("private:12")[0]
        assert failed.status == "failed"
        assert any("執行失敗" in (s.text or "") and failed.task_id in (s.text or "") for s in raised.sent)

        rc = FakeMessage(text="請實作 returncode 失敗", chat_id=13, message_id=3)
        with patch.object(
            bot,
            "relay_turn",
            return_value=RunResult(stdout="", stderr="nope", returncode=2),
        ):
            await bot._on_message(_update(rc, 13), SimpleNamespace())
            await _drain(bot)
        rc_task = TaskRegistry(store_path).list_tasks("private:13")[0]
        assert rc_task.status == "failed"
        assert any(rc_task.task_id in (s.text or "") for s in rc.sent)
        _reset(bot)

    asyncio.run(_run())


def test_startup_marks_inflight_interrupted_without_rerun(monkeypatch, tmp_path: Path) -> None:
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path)
    db = tmp_path / "sessions.sqlite3"
    registry = TaskRegistry(db)
    running = registry.create_task(
        parent_key="private:33", backend="cursor", title="還在跑", status="running"
    )
    queued = registry.create_task(
        parent_key="private:33", backend="cursor", title="排隊中", status="queued"
    )
    done = registry.create_task(
        parent_key="private:33", backend="cursor", title="已完成", status="completed"
    )
    registry.bind_message(
        parent_key="private:33",
        message_id=41,
        task_id=running.task_id,
        backend="cursor",
        role="source",
    )
    changed = registry.interrupt_inflight()
    assert changed == 2
    assert registry.get_task(running.task_id).status == "interrupted"
    assert registry.get_task(queued.task_id).status == "interrupted"
    assert registry.get_task(done.task_id).status == "completed"
    assert registry.get_task_for_message("private:33", 41).task_id == running.task_id

    async def _run():
        _reset(bot)
        with patch.object(bot, "relay_turn") as relay:
            status = FakeMessage(text="/task", chat_id=33, message_id=50)
            await bot._cmd_task(_update(status, 33), SimpleNamespace(args=[running.task_id]))
            body = status.sent[-1].text
            assert running.task_id in body
            assert "interrupted" in body
            relay.assert_not_called()
            follow = FakeMessage(
                text="手動繼續",
                chat_id=33,
                message_id=51,
                reply_to=41,
            )
            relay.return_value = RunResult(stdout="resumed", stderr="", returncode=0)
            await bot._on_message(_update(follow, 33), SimpleNamespace())
            await _drain(bot)
            relay.assert_called_once()
            assert relay.call_args.kwargs["thread_key"] == running.worker_key
            assert relay.call_args.kwargs["prompt"] == "手動繼續"
        _reset(bot)

    asyncio.run(_run())


def test_parent_model_pref_copied_not_leaked(monkeypatch, tmp_path: Path) -> None:
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path)
    store = SessionStore(tmp_path / "sessions.sqlite3")
    store.set_pref("private:44", "model:cursor", "composer-1")

    async def _run():
        _reset(bot)
        with patch.object(
            bot, "relay_turn", return_value=RunResult(stdout="ok", stderr="", returncode=0)
        ):
            msg = FakeMessage(text="請實作複製模型偏好", chat_id=44, message_id=1)
            await bot._on_message(_update(msg, 44), SimpleNamespace())
            await _drain(bot)
        task = TaskRegistry(store.db_path).list_tasks("private:44")[0]
        assert store.get_pref(task.worker_key, "model:cursor") == "composer-1"
        store.set_pref("private:44", "model:cursor", "other-model")
        assert store.get_pref(task.worker_key, "model:cursor") == "composer-1"
        _reset(bot)

    asyncio.run(_run())


def test_unthreaded_continuation_asks_when_ambiguous(monkeypatch, tmp_path: Path) -> None:
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, backend="codex")
    registry = TaskRegistry(tmp_path / "sessions.sqlite3")
    first = registry.create_task(parent_key="private:101", backend="codex", title="A", status="completed")
    second = registry.create_task(parent_key="private:101", backend="codex", title="B", status="completed")
    registry.clear_focus("private:101")
    calls: list[str] = []

    def relay(**kwargs):
        calls.append(kwargs["thread_key"])
        return RunResult(stdout="done", stderr="", returncode=0)

    async def _run():
        _reset(bot)
        with patch.object(bot, "relay_turn", side_effect=relay):
            for text in ("繼續", "接著做", "往下做", "做好了嗎"):
                msg = FakeMessage(text=text, chat_id=101, message_id=hash(text) % 10000 + 1)
                await bot._on_message(_update(msg, 101), SimpleNamespace())
                await _drain(bot)
                body = msg.sent[-1].text
                assert first.task_id in body and second.task_id in body
                assert "/task" in body
        assert calls == []
        _reset(bot)

    asyncio.run(_run())


def test_unthreaded_continuation_resumes_sole_task(monkeypatch, tmp_path: Path) -> None:
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, backend="codex")
    registry = TaskRegistry(tmp_path / "sessions.sqlite3")
    sole = registry.create_task(parent_key="private:77", backend="codex", title="only", status="completed")
    calls: list[dict] = []

    def relay(**kwargs):
        calls.append({"thread_key": kwargs["thread_key"], "prompt": kwargs["prompt"]})
        return RunResult(stdout="done", stderr="", returncode=0)

    async def _run():
        _reset(bot)
        with patch.object(bot, "relay_turn", side_effect=relay):
            msg = FakeMessage(text="繼續", chat_id=77, message_id=1)
            await bot._on_message(_update(msg, 77), SimpleNamespace())
            await _drain(bot)
        assert calls == [{"thread_key": sole.worker_key, "prompt": "繼續"}]
        _reset(bot)

    asyncio.run(_run())


def test_delegate_mentioning_progress_still_opens_new_task(monkeypatch, tmp_path: Path) -> None:
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, backend="codex")
    registry = TaskRegistry(tmp_path / "sessions.sqlite3")
    registry.create_task(parent_key="private:88", backend="codex", title="old", status="completed")
    calls: list[dict] = []

    def relay(**kwargs):
        calls.append({"thread_key": kwargs["thread_key"], "prompt": kwargs["prompt"]})
        return RunResult(stdout="done", stderr="", returncode=0)

    async def _run():
        _reset(bot)
        with patch.object(bot, "relay_turn", side_effect=relay):
            msg = FakeMessage(
                text="請實作並持續監控進度的跨檔背景工作",
                chat_id=88,
                message_id=2,
            )
            await bot._on_message(_update(msg, 88), SimpleNamespace())
            await _drain(bot)
        assert len(calls) == 1
        assert calls[0]["thread_key"].startswith("private:88:job:")
        assert calls[0]["thread_key"] != "private:88"
        _reset(bot)

    asyncio.run(_run())


def test_reply_to_bound_progress_query_is_status_only(monkeypatch, tmp_path: Path) -> None:
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, backend="codex")
    calls: list[str] = []

    def relay(**kwargs):
        calls.append(kwargs["prompt"])
        return RunResult(stdout="done", stderr="", returncode=0)

    async def _run():
        _reset(bot)
        with patch.object(bot, "relay_turn", side_effect=relay):
            src = FakeMessage(text="請實作任務A獨立功能", chat_id=91, message_id=1)
            await bot._on_message(_update(src, 91), SimpleNamespace())
            await _drain(bot)
            task_id = _task_id_from_sent(src)
            before = len(calls)
            probe = FakeMessage(text="現在進度如何", chat_id=91, message_id=2, reply_to=1)
            await bot._on_message(_update(probe, 91), SimpleNamespace())
            await _drain(bot)
            assert len(calls) == before
            body = probe.sent[-1].text
            assert task_id in body
            assert "狀態:" in body
            status_id = probe.sent[-1].message_id
            follow = FakeMessage(text="補上測試", chat_id=91, message_id=3, reply_to=status_id)
            await bot._on_message(_update(follow, 91), SimpleNamespace())
            await _drain(bot)
            assert calls[-1] == "補上測試"
        _reset(bot)

    asyncio.run(_run())


def test_queued_initial_precedes_followup_and_occupancy_one(monkeypatch, tmp_path: Path) -> None:
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, backend="codex")
    calls: list[dict] = []
    hold_a = threading.Event()
    a_started = threading.Event()
    occupancy: dict[str, dict[str, int]] = {}
    occupancy_lock = threading.Lock()

    def relay(**kwargs):
        calls.append({"thread_key": kwargs["thread_key"], "prompt": kwargs["prompt"]})
        key = kwargs["thread_key"]
        with occupancy_lock:
            slot = occupancy.setdefault(key, {"current": 0, "max": 0})
            slot["current"] += 1
            slot["max"] = max(slot["max"], slot["current"])
        try:
            if "任務A" in kwargs["prompt"]:
                a_started.set()
                hold_a.wait(timeout=5)
            return RunResult(stdout=f"done:{kwargs['prompt']}", stderr="", returncode=0)
        finally:
            with occupancy_lock:
                occupancy[key]["current"] -= 1

    async def _run():
        _reset(bot)
        with patch.object(bot, "relay_turn", side_effect=relay):
            msg_a = FakeMessage(text="請實作任務A獨立功能", chat_id=101, message_id=1)
            await bot._on_message(_update(msg_a, 101), SimpleNamespace())
            assert await asyncio.to_thread(a_started.wait, 2)
            msg_b = FakeMessage(text="請實作任務B獨立功能", chat_id=101, message_id=2)
            await bot._on_message(_update(msg_b, 101), SimpleNamespace())
            b_id = _task_id_from_sent(msg_b)
            follow = FakeMessage(
                text="追加 B 的要求",
                chat_id=101,
                message_id=3,
                reply_to=2,
            )
            await bot._on_message(_update(follow, 101), SimpleNamespace())
            assert all("追加 B 的要求" not in c["prompt"] for c in calls)
            assert all("任務B" not in c["prompt"] for c in calls)
            hold_a.set()
            await _drain(bot)
        b_key = f"private:101:job:{b_id}"
        b_prompts = [c["prompt"] for c in calls if c["thread_key"] == b_key]
        assert b_prompts[0] == "請實作任務B獨立功能"
        assert "追加 B 的要求" in b_prompts
        assert b_prompts.index("請實作任務B獨立功能") < b_prompts.index("追加 B 的要求")
        assert occupancy[b_key]["max"] == 1
        assert all(slot["max"] == 1 for slot in occupancy.values())
        _reset(bot)

    asyncio.run(_run())


def test_queued_without_in_memory_initial_fails_visibly(monkeypatch, tmp_path: Path) -> None:
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, backend="codex")
    registry = TaskRegistry(tmp_path / "sessions.sqlite3")
    queued = registry.create_task(parent_key="private:101", backend="codex", title="B", status="queued")
    registry.bind_message(
        parent_key="private:101",
        message_id=2,
        task_id=queued.task_id,
        backend="codex",
        role="source",
    )
    calls: list[str] = []

    def relay(**kwargs):
        calls.append(kwargs["prompt"])
        return RunResult(stdout="done", stderr="", returncode=0)

    async def _run():
        _reset(bot)
        with patch.object(bot, "relay_turn", side_effect=relay):
            msg = FakeMessage(text="追加 B 的要求", chat_id=101, message_id=3, reply_to=2)
            await bot._on_message(_update(msg, 101), SimpleNamespace())
            await _drain(bot)
            body = msg.sent[-1].text
            assert queued.task_id in body
            assert "尚未執行" in body
            assert "手動" in body or "/task" in body
        assert calls == []
        _reset(bot)

    asyncio.run(_run())


def test_status_replies_are_task_bound(monkeypatch, tmp_path: Path) -> None:
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, backend="codex")
    calls: list[dict] = []

    def relay(**kwargs):
        calls.append({"thread_key": kwargs["thread_key"], "prompt": kwargs["prompt"]})
        return RunResult(stdout="done", stderr="", returncode=0)

    async def _run():
        _reset(bot)
        with patch.object(bot, "relay_turn", side_effect=relay):
            src = FakeMessage(text="請實作任務A獨立功能", chat_id=55, message_id=1)
            await bot._on_message(_update(src, 55), SimpleNamespace())
            await _drain(bot)
            task_id = _task_id_from_sent(src)
            status = FakeMessage(text="/task", chat_id=55, message_id=2)
            await bot._cmd_task(_update(status, 55), SimpleNamespace(args=[task_id]))
            body = status.sent[-1].text
            assert task_id in body
            status_id = status.sent[-1].message_id
            follow = FakeMessage(
                text="從狀態回覆繼續",
                chat_id=55,
                message_id=3,
                reply_to=status_id,
            )
            await bot._on_message(_update(follow, 55), SimpleNamespace())
            await _drain(bot)
            assert calls[-1]["thread_key"] == f"private:55:job:{task_id}"
            assert calls[-1]["prompt"] == "從狀態回覆繼續"
        _reset(bot)

    asyncio.run(_run())


def test_task_title_redacts_path_and_credentials(monkeypatch, tmp_path: Path) -> None:
    from tg_cli_relay import telegram_bot as bot

    assert make_task_title("請實作任務A獨立功能") == "請實作任務A獨立功能"
    assert make_task_title("password=example-secret please implement") == "background task"
    assert make_task_title("TGR_FILE:/tmp/secret.pem") == "background task"
    assert "secret.env" not in make_task_title("請實作 /Users/ht/secret.env 的處理並補測試")

    _env(monkeypatch, tmp_path, backend="codex")

    async def _run():
        _reset(bot)
        with patch.object(
            bot, "relay_turn", return_value=RunResult(stdout="ok", stderr="", returncode=0)
        ):
            msg = FakeMessage(
                text="請實作 /Users/ht/secret.env 的處理並補測試",
                chat_id=66,
                message_id=1,
            )
            await bot._on_message(_update(msg, 66), SimpleNamespace())
            await _drain(bot)
        task = TaskRegistry(tmp_path / "sessions.sqlite3").list_tasks("private:66")[0]
        assert task.title == "background task"
        assert all("secret.env" not in (s.text or "") for s in msg.sent)
        _reset(bot)

    asyncio.run(_run())


def test_enqueue_ack_failure_does_not_orphan_queue(monkeypatch, tmp_path: Path) -> None:
    from tg_cli_relay import telegram_bot as bot

    _env(monkeypatch, tmp_path, backend="codex")
    hold_a = threading.Event()
    a_started = threading.Event()

    def relay(**kwargs):
        if "任務A" in kwargs["prompt"]:
            a_started.set()
            hold_a.wait(timeout=5)
        return RunResult(stdout="done", stderr="", returncode=0)

    async def _run():
        _reset(bot)
        original_reply = bot._reply_text_with_retry

        async def fail_queue_ack(message, text, *args, **kwargs):
            if "等待佇列" in text:
                raise RuntimeError("ack failed")
            return await original_reply(message, text, *args, **kwargs)

        with patch.object(bot, "relay_turn", side_effect=relay), patch.object(
            bot, "_reply_text_with_retry", side_effect=fail_queue_ack
        ):
            msg_a = FakeMessage(text="請實作任務A獨立功能", chat_id=12, message_id=1)
            await bot._on_message(_update(msg_a, 12), SimpleNamespace())
            assert await asyncio.to_thread(a_started.wait, 2)
            msg_b = FakeMessage(text="請實作任務B獨立功能", chat_id=12, message_id=2)
            with pytest.raises(RuntimeError, match="ack failed"):
                await bot._on_message(_update(msg_b, 12), SimpleNamespace())
            assert not bot._PENDING_TURNS.get("private:12")
            registry = TaskRegistry(tmp_path / "sessions.sqlite3")
            b_tasks = [t for t in registry.list_tasks("private:12") if t.title.startswith("請實作任務B")]
            assert b_tasks
            assert b_tasks[0].status == "failed"
            hold_a.set()
            await _drain(bot)
        _reset(bot)

    asyncio.run(_run())
