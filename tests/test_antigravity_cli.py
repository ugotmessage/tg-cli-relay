from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from tg_cli_relay import background_callback as bc
from tg_cli_relay.providers import antigravity_cli as agy_mod
from tg_cli_relay.providers.antigravity_cli import (
    ANTIGRAVITY_DEFAULT_MODEL,
    ANTIGRAVITY_MODELS,
    AntigravityCliProvider,
    list_antigravity_models,
    parse_antigravity_models_output,
    parse_antigravity_output,
)
from tg_cli_relay.relay import effective_model_for_backend, relay_turn
from tg_cli_relay.session_store import SessionStore

SID = "76d29f81-320c-42e3-97fc-8f638bc68092"
MARKER = "TGR_BACKGROUND_HANDOFF:task-agy-1:/tmp/handoff-agy-1.md"


def _fake_agy_stream(tmp_path: Path, events: list[dict], *, tail_sleep: float = 0) -> str:
    """Write a fake `agy` that prints stream-json events, then optionally hangs."""
    lines = "\n".join(json.dumps(e) for e in events)
    script = tmp_path / "agy"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        f"print({lines!r}, flush=True)\n"
        f"time.sleep({tail_sleep})\n"
        f"print(json.dumps({{'event':'result','result':{{'conversation_id':{SID!r},'status':'SUCCESS','response':'late\\n'}}}}), flush=True)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


INIT_EVT = {
    "event": "init",
    "conversation_id": SID,
    "init": {"model": "gemini-4-neon-medium", "cwd": "/Users/ht", "permission_mode": "always-proceed"},
}


def _agent_deltas(step_index: int, *chunks: str) -> list[dict]:
    events = []
    for i, chunk in enumerate(chunks):
        events.append(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": SID,
                    "step_index": step_index,
                    "state": "DONE" if i == len(chunks) - 1 else "ACTIVE",
                    "step_type": "agent_response",
                    "text_delta": chunk,
                },
            }
        )
    return events


def test_parse_antigravity_json_output():
    blob = json.dumps(
        {
            "conversation_id": SID,
            "status": "SUCCESS",
            "response": "SMOKE_OK\n",
            "duration_seconds": 2.9,
            "num_turns": 1,
        }
    )
    text, sid, is_err = parse_antigravity_output(blob)
    assert text == "SMOKE_OK"
    assert sid == SID
    assert is_err is False


def test_parse_antigravity_error_status():
    blob = json.dumps({"conversation_id": SID, "status": "ERROR", "response": "quota exceeded"})
    text, sid, is_err = parse_antigravity_output(blob)
    assert text == "quota exceeded"
    assert sid == SID
    assert is_err is True


def test_cli_flags_attach_prompt_with_equals_for_dash_prefix(tmp_path):
    argv_file = tmp_path / "argv.json"
    script = tmp_path / "agy"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"json.dump(sys.argv[1:], open({str(argv_file)!r}, 'w'))\n"
        f"print(json.dumps({{'conversation_id':{SID!r},'status':'SUCCESS','response':'OK\\n'}}))\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    prov = AntigravityCliProvider(
        agy_bin=str(script),
        dangerously_skip_permissions=True,
        model="gemini-4-neon-high",
    )
    res = prov.run_turn(
        workspace=str(tmp_path),
        session_id="prev-sid",
        prompt="--leading-dash prompt\nline2",
    )
    argv = json.loads(argv_file.read_text())
    assert argv[:2] == ["--output-format", "json"]
    assert "--dangerously-skip-permissions" in argv
    assert argv[argv.index("--model") + 1] == "gemini-4-neon-high"
    assert argv[argv.index("--resume") + 1] == "prev-sid"
    assert "--print=--leading-dash prompt\nline2" in argv
    assert res.returncode == 0
    assert res.stdout == "OK"
    assert res.session_id == SID


def test_stream_emits_live_progress_without_duplicating_final(tmp_path):
    progress: list[str] = []
    tool_evt = {
        "event": "step_update",
        "step_update": {
            "conversation_id": SID,
            "step_index": 2,
            "state": "DONE",
            "step_type": "tool",
            "tool_name": "run_command",
        },
    }
    events = [
        INIT_EVT,
        *_agent_deltas(1, "Checking ", "status..."),
        tool_evt,
        *_agent_deltas(3, "All ", "green\n"),
    ]
    script = tmp_path / "agy"
    lines = "\n".join(json.dumps(e) for e in events)
    final_evt = json.dumps(
        {"event": "result", "result": {"conversation_id": SID, "status": "SUCCESS", "response": "All green\n"}}
    )
    script.write_text(
        "#!/usr/bin/env python3\n"
        f"print({lines!r}, flush=True)\n"
        f"print({final_evt!r}, flush=True)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    prov = AntigravityCliProvider(agy_bin=str(script), timeout_seconds=20)
    res = prov.run_turn(
        workspace=str(tmp_path),
        session_id=None,
        prompt="check",
        on_progress=progress.append,
    )
    assert res.returncode == 0
    assert res.session_id == SID
    assert progress == ["Checking status..."]
    assert res.stdout == "All green"
    assert res.stdout_segments is None


def test_stream_detaches_on_exact_handoff_marker(tmp_path):
    calls: list[tuple[str, str, str]] = []
    events = [
        INIT_EVT,
        *_agent_deltas(1, "queued background job"),
        *_agent_deltas(2, MARKER[:20], MARKER[20:]),
    ]
    bin_ = _fake_agy_stream(tmp_path, events, tail_sleep=30)
    prov = AntigravityCliProvider(agy_bin=bin_, timeout_seconds=20)
    res = prov.run_turn(
        workspace=str(tmp_path),
        session_id=None,
        prompt="run long task",
        on_handoff=lambda t, p, s: calls.append((t, p, s)) or True,
    )
    assert calls == [("task-agy-1", "/tmp/handoff-agy-1.md", SID)]
    assert res.handoff_detached is True
    assert res.returncode == 0
    assert res.session_id == SID
    remaining, markers = bc.find_handoff_markers(res)
    assert markers == [("task-agy-1", "/tmp/handoff-agy-1.md")]
    assert remaining == ["queued background job"]


def test_stream_rejected_handoff_marker_keeps_running(tmp_path):
    events = [INIT_EVT, *_agent_deltas(1, MARKER)]
    bin_ = _fake_agy_stream(tmp_path, events)
    prov = AntigravityCliProvider(agy_bin=bin_, timeout_seconds=20)
    res = prov.run_turn(
        workspace=str(tmp_path),
        session_id=None,
        prompt="p",
        on_handoff=lambda *a: False,
    )
    assert res.handoff_detached is False
    assert res.returncode == 0
    assert res.stdout == "late"


def test_stream_killed_before_text_does_not_leak_raw_events(tmp_path):
    tool_evt = {
        "event": "step_update",
        "step_update": {
            "conversation_id": SID,
            "step_index": 1,
            "state": "ACTIVE",
            "step_type": "tool",
            "tool_name": "read_file",
            "tool_info": {"name": "read_file", "parameters": {"Path": "/secret"}},
        },
    }
    bin_ = _fake_agy_stream(tmp_path, [INIT_EVT, tool_evt], tail_sleep=30)
    prov = AntigravityCliProvider(agy_bin=bin_, timeout_seconds=2)
    res = prov.run_turn(
        workspace=str(tmp_path),
        session_id=None,
        prompt="p",
        on_handoff=lambda *a: False,
    )
    assert res.returncode == 124
    assert res.stdout == ""
    assert "conversation_id" not in res.stdout and "tool_info" not in res.stdout


def test_parse_and_list_antigravity_models(tmp_path, monkeypatch):
    raw = (
        "\r⠋ Fetching available models...\r⠙ Fetching available models..."
        "\rgemini-4-neon-high             Gemini 4 Neon (High)\n"
        "gemini-4-neon-medium           Gemini 4 Neon (Medium)\n"
        "claude-sonnet-5-5-high         Claude Sonnet 5.5 (High)\n"
    )
    assert parse_antigravity_models_output(raw) == [
        "gemini-4-neon-high",
        "gemini-4-neon-medium",
        "claude-sonnet-5-5-high",
    ]

    script = tmp_path / "agy"
    script.write_text(f"#!/usr/bin/env python3\nprint({raw!r})\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(agy_mod, "_MODEL_CACHE", None)
    assert list_antigravity_models(str(script), cache_seconds=0) == [
        "gemini-4-neon-high",
        "gemini-4-neon-medium",
        "claude-sonnet-5-5-high",
    ]

    monkeypatch.setattr(agy_mod, "_MODEL_CACHE", None)
    assert list_antigravity_models("/nonexistent/agy", cache_seconds=0) == ANTIGRAVITY_MODELS


def test_registration_freezes_antigravity_backend():
    assert bc.frozen_backend({"backend": "antigravity"}) == "antigravity"


def test_relay_turn_antigravity_persists_session_and_resolves_model(tmp_path, monkeypatch):
    argv_log = tmp_path / "calls.jsonl"
    script = tmp_path / "agy"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"with open({str(argv_log)!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"print(json.dumps({{'conversation_id':{SID!r},'status':'SUCCESS','response':'HELLO\\n'}}))\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setenv("TGR_ANTIGRAVITY_BIN", str(script))
    monkeypatch.setenv("TGR_ANTIGRAVITY_SKIP_PERMISSIONS", "1")
    monkeypatch.delenv("TGR_ANTIGRAVITY_MODEL", raising=False)

    store = SessionStore(tmp_path / "sessions.sqlite3")
    assert effective_model_for_backend(store, "private:1", "antigravity") == ANTIGRAVITY_DEFAULT_MODEL

    r1 = relay_turn(
        thread_key="private:1",
        backend="antigravity",
        prompt="first",
        store=store,
        workspace=str(tmp_path),
    )
    assert r1.returncode == 0
    assert r1.stdout == "HELLO"
    assert store.get("private:1", "antigravity") == SID

    store.set_pref("private:1", "model:antigravity", "gemini-4-neon-high")
    assert effective_model_for_backend(store, "private:1", "antigravity") == "gemini-4-neon-high"

    r2 = relay_turn(
        thread_key="private:1",
        backend="antigravity",
        prompt="second",
        store=store,
        workspace=str(tmp_path),
    )
    assert r2.returncode == 0
    calls = [json.loads(line) for line in argv_log.read_text().splitlines()]
    assert len(calls) == 2
    assert calls[0][calls[0].index("--model") + 1] == ANTIGRAVITY_DEFAULT_MODEL
    assert "--resume" not in calls[0]
    assert "--dangerously-skip-permissions" in calls[0]
    assert calls[1][calls[1].index("--model") + 1] == "gemini-4-neon-high"
    assert calls[1][calls[1].index("--resume") + 1] == SID
