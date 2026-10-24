"""The Relay must never choose an Agent's delegated worker by message semantics."""
from pathlib import Path
from unittest.mock import patch

import pytest

from tg_cli_relay.providers.base import RunResult
from tg_cli_relay.relay import relay_turn
from tg_cli_relay.session_store import SessionStore


@pytest.mark.parametrize("prompt", [
    "請跨多檔重構並跑完整測試與 build",
    "請實作單一欄位的格式驗證",
    "目前有幾個 relay 在跑？",
    "請批次回補資料並跑完整測試",
])
@pytest.mark.parametrize("worker_setting", ["auto", "cursor", "native"])
def test_codex_always_receives_original_request_without_relay_delegation(
    tmp_path: Path, monkeypatch, prompt: str, worker_setting: str
):
    monkeypatch.setenv("TGR_WORKER_PROVIDER", worker_setting)
    monkeypatch.setenv("TGR_CODEX_DELEGATE_TO_CURSOR", "1")
    monkeypatch.setenv("TGR_ROUTER_CONFIG", str(tmp_path / "router.toml"))
    store = SessionStore(tmp_path / "sessions.sqlite3")
    with patch("tg_cli_relay.relay.CursorAgentProvider") as cursor_cls, patch(
        "tg_cli_relay.relay.ClaudeCliProvider"
    ) as claude_cls, patch(
        "tg_cli_relay.relay._run_codex_cursor_worker"
    ) as dispatch, patch(
        "tg_cli_relay.relay.CodexCliProvider"
    ) as codex_cls:
        codex_cls.return_value.run_turn.return_value = RunResult(
            stdout="Agent 自行處理", stderr="", returncode=0, session_id="codex-1"
        )
        result = relay_turn(
            thread_key="private:9:job:abcd1234",
            backend="codex", prompt=prompt, store=store, workspace=str(tmp_path),
        )

    assert result.stdout == "Agent 自行處理"
    assert codex_cls.return_value.run_turn.call_args.kwargs["prompt"] == prompt
    cursor_cls.assert_not_called()
    claude_cls.assert_not_called()
    dispatch.assert_not_called()
    assert store.get("private:9:job:abcd1234:codex-cursor-worker", "cursor") is None
