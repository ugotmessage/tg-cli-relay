from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from tg_cli_relay import background_callback as bc
from tg_cli_relay.providers.claude_cli import ClaudeCliProvider

SID = "11111111-2222-3333-4444-555555555555"
MARKER = "TGR_BACKGROUND_HANDOFF:task-1:/tmp/handoff-task-1.md"


def _fake_claude(tmp_path: Path, events: list[dict], *, tail_sleep: float = 0) -> str:
    """Write a fake `claude` that prints stream-json events, then optionally hangs."""
    lines = "\n".join(json.dumps(e) for e in events)
    script = tmp_path / "claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "sys.stdin.read()\n"
        f"print({lines!r}, flush=True)\n"
        f"time.sleep({tail_sleep})\n"
        "print('{\"type\":\"result\",\"result\":\"late\",\"is_error\":false}', flush=True)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _assistant(text: str, *, parent: str | None = None) -> dict:
    return {
        "type": "assistant",
        "session_id": SID,
        "parent_tool_use_id": parent,
        "message": {"content": [{"type": "text", "text": text}]},
    }


INIT = {"type": "system", "subtype": "init", "session_id": SID}


def test_stream_detaches_on_exact_top_level_marker(tmp_path):
    calls = []
    bin_ = _fake_claude(tmp_path, [INIT, _assistant("queued it"), _assistant(MARKER)], tail_sleep=30)
    prov = ClaudeCliProvider(claude_bin=bin_, timeout_seconds=20)
    res = prov.run_turn(
        workspace=str(tmp_path), session_id=None, prompt="p",
        on_handoff=lambda t, p, s: calls.append((t, p, s)) or True,
    )
    assert calls == [("task-1", "/tmp/handoff-task-1.md", SID)]
    assert res.handoff_detached is True
    assert res.returncode == 0
    assert res.session_id == SID
    remaining, markers = bc.find_handoff_markers(res)
    assert markers == [("task-1", "/tmp/handoff-task-1.md")]
    assert remaining == ["queued it"]


def test_stream_ignores_subagent_marker_and_rejected_marker(tmp_path):
    calls = []
    events = [INIT, _assistant(MARKER, parent="toolu_1"), _assistant("done"),
              {"type": "result", "result": "final answer", "is_error": False, "session_id": SID}]
    bin_ = _fake_claude(tmp_path, events)
    prov = ClaudeCliProvider(claude_bin=bin_, timeout_seconds=20)
    res = prov.run_turn(
        workspace=str(tmp_path), session_id=None, prompt="p",
        on_handoff=lambda t, p, s: calls.append(t) or False,
    )
    assert calls == []  # subagent marker never offered
    assert res.handoff_detached is False
    # The fake prints a trailing "late" result; the last result wins like the CLI.
    assert res.stdout == "late"
    assert res.session_id == SID


def test_stream_rejected_top_level_marker_keeps_running(tmp_path):
    events = [INIT, _assistant(MARKER)]
    bin_ = _fake_claude(tmp_path, events)
    prov = ClaudeCliProvider(claude_bin=bin_, timeout_seconds=20)
    res = prov.run_turn(
        workspace=str(tmp_path), session_id=None, prompt="p",
        on_handoff=lambda t, p, s: False,
    )
    assert res.handoff_detached is False
    assert res.returncode == 0
    assert res.stdout == "late"


def test_stream_uses_stream_json_flags(tmp_path):
    argv_file = tmp_path / "argv.json"
    script = tmp_path / "claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"json.dump(sys.argv[1:], open({str(argv_file)!r}, 'w'))\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'result','result':'ok','is_error':False,'session_id':'s'}))\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    prov = ClaudeCliProvider(claude_bin=str(script), dangerously_skip_permissions=True)
    res = prov.run_turn(workspace=str(tmp_path), session_id="abc", prompt="p",
                        on_handoff=lambda *a: False)
    argv = json.loads(argv_file.read_text())
    assert argv[:4] == ["--print", "--output-format", "stream-json", "--verbose"]
    assert "--dangerously-skip-permissions" in argv
    assert argv[argv.index("--resume") + 1] == "abc"
    assert res.stdout == "ok"


def test_registration_freezes_claude_backend_and_codex_stays_compatible():
    assert bc.frozen_backend({}) == "codex"
    assert bc.frozen_backend({"backend": "claude"}) == "claude"
    with pytest.raises(bc.CallbackError):
        bc.frozen_backend({"backend": "opencode"})


def test_continuation_chain_is_opt_in(monkeypatch):
    monkeypatch.setenv("TGR_BACKGROUND_HANDOFF", "1")
    monkeypatch.delenv("TGR_BACKGROUND_HANDOFF_CHAIN", raising=False)
    assert bc.continuation_chain_enabled() is False
    assert "dispatch exactly the next item" not in bc.build_continuation_prompt({"task_id": "t"})
    monkeypatch.setenv("TGR_BACKGROUND_HANDOFF_CHAIN", "1")
    assert bc.continuation_chain_enabled() is True
    assert "dispatch exactly the next item" in bc.build_continuation_prompt({"task_id": "t"})
    monkeypatch.setenv("TGR_BACKGROUND_HANDOFF", "0")
    assert bc.continuation_chain_enabled() is False


def test_stream_killed_before_text_does_not_leak_raw_events(tmp_path):
    """/stop or timeout before any assistant text must not forward raw stream-json to Telegram."""
    tool = {"type": "assistant", "session_id": SID, "parent_tool_use_id": None,
            "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}}]}}
    result_evt = {"type": "user", "session_id": SID,
                  "message": {"content": [{"type": "tool_result", "content": "secret file body " * 50}]}}
    bin_ = _fake_claude(tmp_path, [INIT, tool, result_evt], tail_sleep=30)
    prov = ClaudeCliProvider(claude_bin=bin_, timeout_seconds=2)
    res = prov.run_turn(workspace=str(tmp_path), session_id=None, prompt="p",
                        on_handoff=lambda t, p, s: False)
    assert res.returncode == 124
    assert res.stdout == ""
    assert "session_id" not in res.stdout and "tool_result" not in res.stdout
