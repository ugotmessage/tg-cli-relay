from __future__ import annotations

from unittest.mock import patch

from tg_cli_relay.providers import cursor_agent
from tg_cli_relay.providers.cursor_agent import (
    CURSOR_DEFAULT_MODEL,
    CursorAgentProvider,
    list_cursor_models,
)


def test_run_turn_defaults_to_auto_model() -> None:
    prov = CursorAgentProvider(model=None)
    with patch("tg_cli_relay.providers.cursor_agent.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = "ok"
        run.return_value.stderr = ""
        prov.run_turn(workspace="/tmp", session_id="chat-1", prompt="hi")
    cmd = run.call_args.args[0]
    model_idx = cmd.index("--model")
    assert cmd[model_idx + 1] == CURSOR_DEFAULT_MODEL


def test_run_turn_honors_explicit_model() -> None:
    prov = CursorAgentProvider(model="gemini-3.1-pro")
    with patch("tg_cli_relay.providers.cursor_agent.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = "ok"
        run.return_value.stderr = ""
        prov.run_turn(workspace="/tmp", session_id=None, prompt="hi")
    cmd = run.call_args.args[0]
    model_idx = cmd.index("--model")
    assert cmd[model_idx + 1] == "gemini-3.1-pro"


def test_live_model_list_parses_new_cursor_models() -> None:
    cursor_agent._MODEL_CACHE = None
    output = """Available models

auto - Auto (current, default)
composer-2.5 - Composer 2.5
claude-opus-5-5-max - Claude Opus 5.5 1M Max
gpt-5.6-sol-high - GPT-5.6 Sol 1M High
"""
    with patch("tg_cli_relay.providers.cursor_agent.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = output
        run.return_value.stderr = ""
        models = list_cursor_models("agent")
    assert models == ["auto", "composer-2.5", "claude-opus-5-5-max", "gpt-5.6-sol-high"]
    assert run.call_args.args[0] == ["agent", "--list-models"]


def test_live_model_list_falls_back_when_cli_fails() -> None:
    cursor_agent._MODEL_CACHE = None
    with patch("tg_cli_relay.providers.cursor_agent.subprocess.run") as run:
        run.return_value.returncode = 1
        run.return_value.stdout = ""
        run.return_value.stderr = "offline"
        models = list_cursor_models("agent")
    assert models == cursor_agent.CURSOR_MODELS
