from __future__ import annotations

from unittest.mock import patch

from tg_cli_relay.providers.cursor_agent import CURSOR_DEFAULT_MODEL, CursorAgentProvider


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
