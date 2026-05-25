from __future__ import annotations

from pathlib import Path

from tg_cli_relay.providers.base import RunResult
from tg_cli_relay.providers.claude_cli import ClaudeCliProvider, parse_claude_output
from tg_cli_relay.providers.codex_cli import CodexCliProvider, parse_session_id_from_jsonl
from tg_cli_relay.providers.cursor_agent import CursorAgentProvider
from tg_cli_relay.providers.opencode_cli import OpencodeCliProvider, parse_opencode_output
from tg_cli_relay.session_store import Backend, SessionStore


def _default_db_path() -> Path:
    raw = __import__("os").environ.get("TGR_SESSION_DB")
    if raw:
        return Path(raw)
    return Path("data") / "sessions.sqlite3"


def _default_workspace() -> str:
    import os

    w = os.environ.get("TGR_DEFAULT_WORKSPACE", "").strip()
    if not w:
        raise RuntimeError("請設定環境變數 TGR_DEFAULT_WORKSPACE 指向 git 工作區")
    return w


def _model_pref_key(backend: Backend) -> str:
    return f"model:{backend}"


def relay_turn(
    *,
    thread_key: str,
    backend: Backend,
    prompt: str,
    store: SessionStore | None = None,
    workspace: str | None = None,
) -> RunResult:
    """依 thread_key 接續或建立後端 session，送出一輪 prompt。"""
    ws = workspace or _default_workspace()
    st = store or SessionStore(_default_db_path())

    if backend == "cursor":
        return _relay_cursor(st, thread_key, ws, prompt)
    if backend == "codex":
        return _relay_codex(st, thread_key, ws, prompt)
    if backend == "claude":
        return _relay_claude(st, thread_key, ws, prompt)
    if backend == "opencode":
        return _relay_opencode(st, thread_key, ws, prompt)
    raise ValueError(f"未知後端: {backend}")


def _relay_cursor(store: SessionStore, thread_key: str, workspace: str, prompt: str) -> RunResult:
    import os

    bin_name = os.environ.get("TGR_CURSOR_AGENT_BIN", "agent").strip() or "agent"
    model = store.get_pref(thread_key, _model_pref_key("cursor")) or store.get_pref(thread_key, "model")
    prov = CursorAgentProvider(agent_bin=bin_name, model=model)
    sid = store.get(thread_key, "cursor")
    if not sid:
        sid = prov.create_session()
        store.upsert(thread_key, "cursor", sid, workspace=workspace)
    return prov.run_turn(workspace=workspace, session_id=sid, prompt=prompt)


def _relay_claude(store: SessionStore, thread_key: str, workspace: str, prompt: str) -> RunResult:
    import os

    bin_name = os.environ.get("TGR_CLAUDE_BIN", "claude").strip() or "claude"
    skip_perms = os.environ.get("TGR_CLAUDE_SKIP_PERMISSIONS", "").strip().lower() in ("1", "true", "yes")
    model = store.get_pref(thread_key, _model_pref_key("claude")) or store.get_pref(thread_key, "model")
    prov = ClaudeCliProvider(claude_bin=bin_name, dangerously_skip_permissions=skip_perms, model=model)
    sid = store.get(thread_key, "claude")
    raw = prov.run_turn(workspace=workspace, session_id=sid, prompt=prompt)
    display_text, new_sid = parse_claude_output(raw.stdout)
    if new_sid and new_sid != sid:
        store.upsert(thread_key, "claude", new_sid, workspace=workspace)
    return RunResult(stdout=display_text, stderr=raw.stderr, returncode=raw.returncode)


def _relay_opencode(store: SessionStore, thread_key: str, workspace: str, prompt: str) -> RunResult:
    import os

    bin_name = os.environ.get("TGR_OPENCODE_BIN", "opencode").strip() or "opencode"
    model = store.get_pref(thread_key, _model_pref_key("opencode")) or store.get_pref(thread_key, "model")
    prov = OpencodeCliProvider(opencode_bin=bin_name, model=model)
    sid = store.get(thread_key, "opencode")
    raw = prov.run_turn(workspace=workspace, session_id=sid, prompt=prompt)
    display_text, new_sid = parse_opencode_output(raw.stdout)
    # 優先存真正的 session id；若 JSON 沒有則存 marker 讓下次加 -c
    stored_sid = new_sid or "active"
    if stored_sid != sid:
        store.upsert(thread_key, "opencode", stored_sid, workspace=workspace)
    return RunResult(stdout=display_text, stderr=raw.stderr, returncode=raw.returncode)


def _relay_codex(store: SessionStore, thread_key: str, workspace: str, prompt: str) -> RunResult:
    import os

    bin_name = os.environ.get("TGR_CODEX_BIN", "codex").strip() or "codex"
    model = store.get_pref(thread_key, _model_pref_key("codex")) or store.get_pref(thread_key, "model")
    prov = CodexCliProvider(codex_bin=bin_name, model=model)
    sid = store.get(thread_key, "codex")
    res = prov.run_turn(workspace=workspace, session_id=sid, prompt=prompt)
    if sid is None:
        new_sid = parse_session_id_from_jsonl(res.stdout)
        if new_sid:
            store.upsert(thread_key, "codex", new_sid, workspace=workspace)
    return res
