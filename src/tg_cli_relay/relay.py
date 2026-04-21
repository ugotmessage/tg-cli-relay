from __future__ import annotations

from pathlib import Path

from tg_cli_relay.providers.base import RunResult
from tg_cli_relay.providers.codex_cli import CodexCliProvider, parse_session_id_from_jsonl
from tg_cli_relay.providers.cursor_agent import CursorAgentProvider
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
    raise ValueError(f"未知後端: {backend}")


def _relay_cursor(store: SessionStore, thread_key: str, workspace: str, prompt: str) -> RunResult:
    import os

    bin_name = os.environ.get("TGR_CURSOR_AGENT_BIN", "agent").strip() or "agent"
    prov = CursorAgentProvider(agent_bin=bin_name)
    sid = store.get(thread_key, "cursor")
    if not sid:
        sid = prov.create_session()
        store.upsert(thread_key, "cursor", sid, workspace=workspace)
    return prov.run_turn(workspace=workspace, session_id=sid, prompt=prompt)


def _relay_codex(store: SessionStore, thread_key: str, workspace: str, prompt: str) -> RunResult:
    import os

    bin_name = os.environ.get("TGR_CODEX_BIN", "codex").strip() or "codex"
    prov = CodexCliProvider(codex_bin=bin_name)
    sid = store.get(thread_key, "codex")
    res = prov.run_turn(workspace=workspace, session_id=sid, prompt=prompt)
    if sid is None:
        new_sid = parse_session_id_from_jsonl(res.stdout)
        if new_sid:
            store.upsert(thread_key, "codex", new_sid, workspace=workspace)
    return res
