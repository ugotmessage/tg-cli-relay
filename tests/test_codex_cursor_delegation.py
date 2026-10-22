from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from tg_cli_relay.providers.base import RunResult
from tg_cli_relay.relay import relay_turn
from tg_cli_relay.session_store import SessionStore


def test_heavy_codex_task_delegates_to_cursor_then_codex(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TGR_CODEX_DELEGATE_TO_CURSOR", "1")
    monkeypatch.setenv("TGR_ROUTER_CONFIG", str(tmp_path / "missing-router.toml"))
    store = SessionStore(tmp_path / "sessions.sqlite3")
    cursor_result = RunResult(stdout="已修改兩個 scope 並跑完測試", stderr="", returncode=0)
    codex_result = RunResult(stdout="驗收完成", stderr="", returncode=0, session_id="codex-1")

    with patch("tg_cli_relay.relay.CursorAgentProvider") as cursor_cls, patch(
        "tg_cli_relay.relay.CodexCliProvider"
    ) as codex_cls:
        cursor_cls.return_value.create_session.return_value = "cursor-worker-1"
        cursor_cls.return_value.run_turn.return_value = cursor_result
        codex_cls.return_value.run_turn.return_value = codex_result
        result = relay_turn(
            thread_key="chat:1",
            backend="codex",
            prompt="請跨多檔重構並跑完整測試與 build",
            store=store,
            workspace=str(tmp_path),
        )

    assert result.stdout == "驗收完成"
    assert cursor_cls.call_args.kwargs["timeout_seconds"] == 3600
    cursor_prompt = cursor_cls.return_value.run_turn.call_args.kwargs["prompt"]
    assert "Worker for a Codex Orchestrator" in cursor_prompt
    codex_prompt = codex_cls.return_value.run_turn.call_args.kwargs["prompt"]
    assert "Cursor Worker report" in codex_prompt
    assert "Codex Reviewer and Orchestrator" in codex_prompt
    assert "Do not redo routine coding" in codex_prompt
    assert "review ONLY this current request" in codex_prompt
    assert "Do not treat historical tasks" in codex_prompt
    assert "已修改兩個 scope 並跑完測試" in codex_prompt
    assert store.get("chat:1:codex-cursor-worker", "cursor") == "cursor-worker-1"
    assert cursor_cls.return_value.run_turn.call_args.kwargs["session_id"] == "cursor-worker-1"


def test_single_scope_coding_delegates_to_cursor(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TGR_CODEX_DELEGATE_TO_CURSOR", "1")
    monkeypatch.setenv("TGR_ROUTER_CONFIG", str(tmp_path / "missing-router.toml"))
    store = SessionStore(tmp_path / "sessions.sqlite3")

    with patch("tg_cli_relay.relay.CursorAgentProvider") as cursor_cls, patch(
        "tg_cli_relay.relay.CodexCliProvider"
    ) as codex_cls:
        cursor_cls.return_value.create_session.return_value = "cursor-coding-1"
        cursor_cls.return_value.run_turn.return_value = RunResult(
            stdout="實作完成並通過測試", stderr="", returncode=0
        )
        codex_cls.return_value.run_turn.return_value = RunResult(
            stdout="審核通過", stderr="", returncode=0
        )
        relay_turn(
            thread_key="chat:coding",
            backend="codex",
            prompt="請實作單一欄位的格式驗證",
            store=store,
            workspace=str(tmp_path),
        )

    cursor_cls.return_value.run_turn.assert_called_once()
    codex_prompt = codex_cls.return_value.run_turn.call_args.kwargs["prompt"]
    assert "Codex Reviewer and Orchestrator" in codex_prompt


def test_direct_codex_task_does_not_call_cursor(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TGR_CODEX_DELEGATE_TO_CURSOR", "1")
    monkeypatch.setenv("TGR_ROUTER_CONFIG", str(tmp_path / "missing-router.toml"))
    store = SessionStore(tmp_path / "sessions.sqlite3")

    with patch("tg_cli_relay.relay.CursorAgentProvider") as cursor_cls, patch(
        "tg_cli_relay.relay.CodexCliProvider"
    ) as codex_cls:
        codex_cls.return_value.run_turn.return_value = RunResult(
            stdout="目前一個", stderr="", returncode=0
        )
        relay_turn(
            thread_key="chat:2",
            backend="codex",
            prompt="目前有幾個 relay 在跑？",
            store=store,
            workspace=str(tmp_path),
        )

    cursor_cls.assert_not_called()
    assert codex_cls.return_value.run_turn.call_args.kwargs["prompt"] == "目前有幾個 relay 在跑？"


def test_cursor_failure_falls_back_to_original_codex_prompt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TGR_CODEX_DELEGATE_TO_CURSOR", "1")
    monkeypatch.setenv("TGR_ROUTER_CONFIG", str(tmp_path / "missing-router.toml"))
    store = SessionStore(tmp_path / "sessions.sqlite3")

    with patch("tg_cli_relay.relay.CursorAgentProvider") as cursor_cls, patch(
        "tg_cli_relay.relay.CodexCliProvider"
    ) as codex_cls:
        cursor_cls.return_value.create_session.return_value = "cursor-worker-2"
        cursor_cls.return_value.run_turn.return_value = RunResult(
            stdout="", stderr="quota exhausted", returncode=1
        )
        codex_cls.return_value.run_turn.return_value = RunResult(
            stdout="Codex fallback", stderr="", returncode=0
        )
        prompt = "請批次回補資料並跑完整測試"
        relay_turn(
            thread_key="chat:3",
            backend="codex",
            prompt=prompt,
            store=store,
            workspace=str(tmp_path),
        )

    assert codex_cls.return_value.run_turn.call_args.kwargs["prompt"] == prompt


def test_orchestrator_prompt_scopes_nested_worker_to_current_task(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TGR_CODEX_DELEGATE_TO_CURSOR", "1")
    monkeypatch.setenv("TGR_ROUTER_CONFIG", str(tmp_path / "missing-router.toml"))
    store = SessionStore(tmp_path / "sessions.sqlite3")

    with patch("tg_cli_relay.relay.CursorAgentProvider") as cursor_cls, patch(
        "tg_cli_relay.relay.CodexCliProvider"
    ) as codex_cls:
        cursor_cls.return_value.create_session.return_value = "cursor-task-worker"
        cursor_cls.return_value.run_turn.return_value = RunResult(
            stdout="current task only", stderr="", returncode=0
        )
        codex_cls.return_value.run_turn.return_value = RunResult(
            stdout="review", stderr="", returncode=0
        )
        relay_turn(
            thread_key="private:9:job:abcd1234",
            backend="codex",
            prompt="請跨多檔重構並跑完整測試",
            store=store,
            workspace=str(tmp_path),
        )

    assert store.get("private:9:job:abcd1234:codex-cursor-worker", "cursor") == "cursor-task-worker"
    assert cursor_cls.return_value.run_turn.call_args.kwargs["session_id"] == "cursor-task-worker"
    assert "current task only" in codex_cls.return_value.run_turn.call_args.kwargs["prompt"]
    assert "Current task id: abcd1234" in codex_cls.return_value.run_turn.call_args.kwargs["prompt"]
    assert "Do not treat historical tasks" in codex_cls.return_value.run_turn.call_args.kwargs["prompt"]
    assert "Do not deploy, restart services" in cursor_cls.return_value.run_turn.call_args.kwargs["prompt"]


def test_delegation_can_be_disabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TGR_CODEX_DELEGATE_TO_CURSOR", "0")
    store = SessionStore(tmp_path / "sessions.sqlite3")

    with patch("tg_cli_relay.relay.CursorAgentProvider") as cursor_cls, patch(
        "tg_cli_relay.relay.CodexCliProvider"
    ) as codex_cls:
        codex_cls.return_value.run_turn.return_value = RunResult(stdout="done", stderr="", returncode=0)
        relay_turn(
            thread_key="chat:4",
            backend="codex",
            prompt="請跨多檔重構並跑完整測試",
            store=store,
            workspace=str(tmp_path),
        )

    cursor_cls.assert_not_called()
