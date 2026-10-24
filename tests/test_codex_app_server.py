"""Focused protocol and routing regressions; no Codex binary/API required."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tg_cli_relay.providers.base import RunResult
from tg_cli_relay.providers.codex_app_server import (
    AppServerClient, CodexAppServerError, _run_turn,
)
from tg_cli_relay.relay import relay_turn
from tg_cli_relay.session_store import SessionStore
from tg_cli_relay.worker_selector import choose_codex_worker


class FakeAppServer:
    def __init__(self, *, failed=False):
        self.failed = failed
        self.calls = []
        self.inbox = asyncio.Queue()

    async def start(self):
        return None

    async def request(self, method, params, **kwargs):
        self.calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "t1"}}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        if method == "turn/start":
            self.inbox.put_nowait({
                "method": "item/completed",
                "params": {
                    "threadId": params["threadId"], "turnId": "r1",
                    "item": {"type": "agentMessage", "text": "修正完成"},
                },
            })
            self.inbox.put_nowait({
                "method": "turn/completed",
                "params": {"threadId": params["threadId"], "turnId": "r1",
                           "turn": {"status": "failed" if self.failed else "completed",
                                    "error": {"message": "failed"} if self.failed else None}},
            })
            return {"turn": {"id": "r1"}}
        raise AssertionError(method)

    def subscribe(self):
        return self.inbox

    def unsubscribe(self, inbox):
        assert inbox is self.inbox


@pytest.mark.parametrize("sid,thread_action", [(None, "thread/start"), ("t1", "thread/resume")])
def test_native_app_server_thread_persists(sid, thread_action):
    async def run():
        client = FakeAppServer()
        result = await _run_turn(client, workspace="/tmp/repo", session_id=sid,
                                 prompt="請修復", model="gpt", bypass=False,
                                 timeout=1, on_progress=None)
        assert result == RunResult(stdout="修正完成", stderr="", returncode=0, session_id="t1",
                                   stdout_segments=None)
        assert client.calls[0][0] == thread_action
        assert client.calls[1][0] == "turn/start"
        assert client.calls[1][1]["model"] == "gpt"
    asyncio.run(run())


def test_app_server_failure_does_not_mark_success():
    async def run():
        client = FakeAppServer(failed=True)
        result = await _run_turn(client, workspace="/tmp/repo", session_id=None,
                                 prompt="修復", model=None, bypass=False,
                                 timeout=1, on_progress=None)
        assert result.returncode != 0
        assert result.stderr == "failed"
    asyncio.run(run())


def test_app_server_correlates_responses_and_declines_interactive_approvals():
    async def run():
        client = AppServerClient("codex")
        reader = asyncio.StreamReader()
        client.process = SimpleNamespace(stdout=reader)
        client._send = AsyncMock()
        waiter = asyncio.get_running_loop().create_future()
        client.pending[7] = waiter
        inbox = client.subscribe()
        reader.feed_data(json.dumps({"id": 7, "result": {"ok": True}}).encode() + b"\n")
        reader.feed_data(json.dumps({"id": 8, "method": "item/fileChange/requestApproval",
                                     "params": {}}).encode() + b"\n")
        reader.feed_data(json.dumps({"method": "turn/completed",
                                     "params": {"threadId": "t1"}}).encode() + b"\n")
        reader.feed_eof()
        await client._read()
        assert waiter.result() == {"ok": True}
        assert client._send.call_args.args[0] == {"id": 8, "result": {"decision": "decline"}}
        assert (await inbox.get())["method"] == "turn/completed"
    asyncio.run(run())


def test_worker_auto_falls_back_to_native_without_cursor(monkeypatch):
    monkeypatch.delenv("TGR_CODEX_DELEGATE_TO_CURSOR", raising=False)
    monkeypatch.delenv("TGR_WORKER_PROVIDER", raising=False)
    with patch("tg_cli_relay.worker_selector._installed", return_value=False):
        choice = choose_codex_worker()
    assert choice.mode == "native"
    assert choice.provider is None


def test_codex_app_server_opt_in_and_native_delegation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TGR_CODEX_TRANSPORT", "app-server")
    monkeypatch.setenv("TGR_WORKER_PROVIDER", "native")
    monkeypatch.setenv("TGR_ROUTER_CONFIG", str(tmp_path / "missing-router.toml"))
    store = SessionStore(tmp_path / "sessions.sqlite3")
    store.upsert("private:1", "codex", "existing-thread")
    with patch("tg_cli_relay.relay.CodexAppServerProvider") as app, patch(
        "tg_cli_relay.relay.CodexCliProvider"
    ) as cli, patch("tg_cli_relay.relay.CursorAgentProvider") as cursor:
        app.return_value.run_turn.return_value = RunResult(
            stdout="完成", stderr="", returncode=0, session_id="existing-thread"
        )
        result = relay_turn(
            thread_key="private:1", backend="codex",
            prompt="請批次修復補檔並跑測試", store=store, workspace=str(tmp_path)
        )
    assert result.stdout == "完成"
    app.return_value.run_turn.assert_called_once()
    assert app.return_value.run_turn.call_args.kwargs["session_id"] == "existing-thread"
    assert "native agent tools" in app.return_value.run_turn.call_args.kwargs["prompt"]
    cli.assert_not_called()
    cursor.assert_not_called()


def test_invalid_transport_rejected_before_work(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TGR_CODEX_TRANSPORT", "typo")
    monkeypatch.setenv("TGR_WORKER_PROVIDER", "off")
    with pytest.raises(ValueError, match="TGR_CODEX_TRANSPORT"):
        relay_turn(
            thread_key="private:99", backend="codex", prompt="hi",
            store=SessionStore(tmp_path / "sessions.sqlite3"),
            workspace=str(tmp_path)
        )


def test_legacy_disable_does_not_force_subagent_prompt(monkeypatch):
    monkeypatch.delenv("TGR_WORKER_PROVIDER", raising=False)
    monkeypatch.setenv("TGR_CODEX_DELEGATE_TO_CURSOR", "0")
    assert choose_codex_worker().mode == "off"
