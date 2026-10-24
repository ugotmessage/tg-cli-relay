"""Primary provider routing turns must not spawn extra delegated workers."""
from pathlib import Path
from unittest.mock import patch

from tg_cli_relay.providers.base import RunResult
from tg_cli_relay.relay import relay_turn
from tg_cli_relay.session_store import SessionStore


def test_codex_routing_turn_skips_external_worker(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TGR_WORKER_PROVIDER", "cursor")
    monkeypatch.setenv("TGR_ROUTER_CONFIG", str(tmp_path / "missing.toml"))
    store = SessionStore(tmp_path / "sessions.sqlite3")
    text = "請實作多個功能；僅作 routing-only 判斷"
    with patch("tg_cli_relay.relay._run_codex_cursor_worker") as worker, patch(
        "tg_cli_relay.relay.CodexCliProvider"
    ) as cli:
        cli.return_value.run_turn.return_value = RunResult('{"action":"ask"}', "", 0)
        result = relay_turn(
            thread_key="private:101", backend="codex",
            prompt=text, store=store, workspace=str(tmp_path),
            routing_only=True,
        )
    assert result.returncode == 0
    worker.assert_not_called()
    assert cli.return_value.run_turn.call_args.kwargs["prompt"] == text


def test_cursor_routing_turn_skips_harness_prefix(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TGR_ROUTER_CONFIG", str(tmp_path / "missing.toml"))
    store = SessionStore(tmp_path / "sessions.sqlite3")
    with patch("tg_cli_relay.relay.CursorAgentProvider") as cursor:
        cursor.return_value.create_session.return_value = "sess-101"
        cursor.return_value.run_turn.return_value = RunResult('{"action":"new"}', "", 0)
        relay_turn(
            thread_key="private:101", backend="cursor",
            prompt="這是純粹的 task resolution request", store=store,
            workspace=str(tmp_path), routing_only=True,
        )
    assert cursor.return_value.run_turn.call_args.kwargs["prompt"] == (
        "這是純粹的 task resolution request"
    )
