"""Real subprocess deadline tests (sub-second), not synthetic routing mocks."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tg_cli_relay.providers.codex_cli import CodexCliProvider
from tg_cli_relay.providers.cursor_agent import CursorAgentProvider
from tg_cli_relay.providers.claude_cli import ClaudeCliProvider
from tg_cli_relay.providers.opencode_cli import OpencodeCliProvider


@pytest.mark.skipif(os.name == "nt", reason="fake executable script uses POSIX shebang")
@pytest.mark.parametrize("backend", ["codex-stream", "codex-plain", "cursor", "claude", "opencode"])
def test_real_cli_turn_timeout_bounded_and_partial_session_preserved(
    tmp_path: Path, backend: str
):
    fake = tmp_path / "fake_agent"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "if '--warmup' in sys.argv: sys.exit(0)\n"
        "sys.stdout.write('{\"type\":\"thread.started\",\"thread_id\":\"partial-session\"}\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    # macOS can spend longer than the deadline inspecting a new executable.
    # Warm that path before measuring the provider's running-turn timeout.
    subprocess.run([str(fake), "--warmup"], check=True, timeout=5)
    if backend.startswith("codex"):
        provider = CodexCliProvider(codex_bin=str(fake), timeout_seconds=0.25)
        kwargs = {"on_progress": lambda _: None} if backend == "codex-stream" else {}
    elif backend == "cursor":
        provider = CursorAgentProvider(agent_bin=str(fake), timeout_seconds=0.25)
        kwargs = {}
    elif backend == "claude":
        provider = ClaudeCliProvider(claude_bin=str(fake), timeout_seconds=0.25)
        kwargs = {}
    else:
        provider = OpencodeCliProvider(opencode_bin=str(fake), timeout_seconds=0.25)
        kwargs = {}
    started = time.monotonic()
    result = provider.run_turn(
        workspace=str(tmp_path), session_id="existing", prompt="測試",
        thread_key=f"test:timeout:{backend}", **kwargs,
    )
    assert time.monotonic() - started < 5
    assert result.returncode == 124
    if backend.startswith("codex"):
        assert result.session_id == "partial-session"


def test_relay_timeout_survives_telegram_delivery_error(monkeypatch, tmp_path: Path):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import patch, AsyncMock

    from tg_cli_relay import telegram_bot as bot
    from tg_cli_relay.providers.base import RunResult
    from test_task_isolation import FakeMessage, _env, _reset, _update

    _env(monkeypatch, tmp_path, backend="claude")
    calls = []

    def relay(**kw):
        calls.append(kw["prompt"])
        if len(calls) == 1:
            return RunResult("", "timeout", 124)
        return RunResult("done", "", 0)

    async def run():
        _reset(bot)
        bot._ACTIVE_PARENT_BACKENDS.clear()
        bot._ACTIVE_PARENT_REQUESTS.clear()
        msg = FakeMessage(text="第一輪請修 API", chat_id=88, message_id=12)
        try:
            with patch.object(bot, "relay_turn", side_effect=relay), patch.object(
                bot, "_send_relay_output", new_callable=AsyncMock,
                side_effect=[RuntimeError("TG temporarily down"), None],
            ):
                await bot._on_message(_update(msg, 88), SimpleNamespace())
                while bot._BACKGROUND_TASKS:
                    await asyncio.gather(*tuple(bot._BACKGROUND_TASKS), return_exceptions=True)
        finally:
            _reset(bot)
            bot._ACTIVE_PARENT_BACKENDS.clear()
            bot._ACTIVE_PARENT_REQUESTS.clear()

    asyncio.run(run())
    assert len(calls) == 2
    assert "automatic timeout recovery" in calls[1]
