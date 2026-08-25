from __future__ import annotations

from pathlib import Path
from typing import Callable

from tg_cli_relay.providers.base import RunResult
from tg_cli_relay.providers.claude_cli import ClaudeCliProvider, parse_claude_output
from tg_cli_relay.providers.codex_cli import CodexCliProvider
from tg_cli_relay.providers.cursor_agent import CursorAgentProvider
from tg_cli_relay.providers.opencode_cli import OpencodeCliProvider, parse_opencode_output
from tg_cli_relay.session_store import Backend, SessionStore

DEFAULT_CURSOR_HARNESS_PREFIX = (
    "Before acting, enforce Mandatory Agent Harness Workflow "
    "(~/.cursor/rules/agent-harness-workflow.mdc). "
    "If any trigger matches, you MUST act only as Orchestrator and delegate "
    "implementation/investigation to Workers. "
    "TG relay: main session Shell/Write/StrReplace/Delete combined max 2; "
    "reply in 繁體中文 with at least 2 sentences.\n\n"
)


def apply_cursor_harness_prefix(prompt: str) -> str:
    """Prepend harness enforcement instructions for Cursor TG relay turns."""
    import os

    flag = os.environ.get("TGR_CURSOR_HARNESS_PREFIX", "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return prompt
    custom = os.environ.get("TGR_CURSOR_PROMPT_PREFIX", "").strip()
    prefix = custom if custom else DEFAULT_CURSOR_HARNESS_PREFIX
    if not prefix.endswith("\n"):
        prefix += "\n"
    return f"{prefix}{prompt}"


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


def resolve_cursor_model(store: SessionStore, thread_key: str) -> str:
    """Cursor relay 使用的模型；預設 auto，僅 thread 或 env 明確指定時才覆寫。"""
    import os

    explicit = store.get_pref(thread_key, _model_pref_key("cursor"))
    if not explicit:
        explicit = os.environ.get("TGR_CURSOR_MODEL", "").strip()
    if not explicit:
        return "auto"
    return "auto" if explicit.upper() == "AUTO" else explicit


def effective_model_for_backend(store: SessionStore, thread_key: str, backend: Backend) -> str:
    if backend == "cursor":
        return resolve_cursor_model(store, thread_key)
    model = store.get_pref(thread_key, _model_pref_key(backend))
    return model or "(預設)"


def _get_model(store: SessionStore, thread_key: str, backend: Backend) -> str | None:
    if backend == "cursor":
        return resolve_cursor_model(store, thread_key)
    return store.get_pref(thread_key, _model_pref_key(backend))


def relay_turn(
    *,
    thread_key: str,
    backend: Backend,
    prompt: str,
    store: SessionStore | None = None,
    workspace: str | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> RunResult:
    """依 thread_key 接續或建立後端 session，送出一輪 prompt。"""
    ws = workspace or _default_workspace()
    st = store or SessionStore(_default_db_path())

    if backend == "cursor":
        return _relay_cursor(st, thread_key, ws, prompt)
    if backend == "codex":
        return _relay_codex(st, thread_key, ws, prompt, on_progress=on_progress)
    if backend == "claude":
        return _relay_claude(st, thread_key, ws, prompt)
    if backend == "opencode":
        return _relay_opencode(st, thread_key, ws, prompt)
    raise ValueError(f"未知後端: {backend}")


def _relay_cursor(store: SessionStore, thread_key: str, workspace: str, prompt: str) -> RunResult:
    import os

    bin_name = os.environ.get("TGR_CURSOR_AGENT_BIN", "agent").strip() or "agent"
    timeout_raw = os.environ.get("TGR_CURSOR_TIMEOUT", "600").strip() or "600"
    timeout_seconds = int(timeout_raw)
    model = _get_model(store, thread_key, "cursor")
    prov = CursorAgentProvider(agent_bin=bin_name, model=model, timeout_seconds=timeout_seconds)
    sid = store.get(thread_key, "cursor")
    if not sid:
        sid = prov.create_session()
        store.upsert(thread_key, "cursor", sid, workspace=workspace)
    return prov.run_turn(
        workspace=workspace,
        session_id=sid,
        prompt=apply_cursor_harness_prefix(prompt),
    )


def _relay_claude(store: SessionStore, thread_key: str, workspace: str, prompt: str) -> RunResult:
    import os

    bin_name = os.environ.get("TGR_CLAUDE_BIN", "claude").strip() or "claude"
    skip_perms = os.environ.get("TGR_CLAUDE_SKIP_PERMISSIONS", "").strip().lower() in ("1", "true", "yes")
    timeout_raw = os.environ.get("TGR_CLAUDE_TIMEOUT", "1800").strip() or "1800"
    timeout_seconds = int(timeout_raw)
    run_as = os.environ.get("TGR_CLAUDE_RUN_AS", "").strip() or None
    model = _get_model(store, thread_key, "claude")
    prov = ClaudeCliProvider(
        claude_bin=bin_name,
        dangerously_skip_permissions=skip_perms,
        run_as_user=run_as,
        model=model,
        timeout_seconds=timeout_seconds,
    )
    sid = store.get(thread_key, "claude")
    raw = prov.run_turn(workspace=workspace, session_id=sid, prompt=prompt)
    display_text, new_sid = parse_claude_output(raw.stdout)
    if new_sid and new_sid != sid:
        store.upsert(thread_key, "claude", new_sid, workspace=workspace)
    return RunResult(stdout=display_text, stderr=raw.stderr, returncode=raw.returncode)


def _relay_opencode(store: SessionStore, thread_key: str, workspace: str, prompt: str) -> RunResult:
    import os

    bin_name = os.environ.get("TGR_OPENCODE_BIN", "opencode").strip() or "opencode"
    model = _get_model(store, thread_key, "opencode")
    prov = OpencodeCliProvider(opencode_bin=bin_name, model=model)
    sid = store.get(thread_key, "opencode")
    raw = prov.run_turn(workspace=workspace, session_id=sid, prompt=prompt)
    display_text, new_sid = parse_opencode_output(raw.stdout)
    # 優先存真正的 session id；若 JSON 沒有則存 marker 讓下次加 -c
    stored_sid = new_sid or "active"
    if stored_sid != sid:
        store.upsert(thread_key, "opencode", stored_sid, workspace=workspace)
    return RunResult(stdout=display_text, stderr=raw.stderr, returncode=raw.returncode)


def _relay_codex(
    store: SessionStore,
    thread_key: str,
    workspace: str,
    prompt: str,
    *,
    on_progress: Callable[[str], None] | None = None,
) -> RunResult:
    import os

    bin_name = os.environ.get("TGR_CODEX_BIN", "codex").strip() or "codex"
    bypass = os.environ.get("TGR_CODEX_BYPASS_APPROVALS_AND_SANDBOX", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    model = _get_model(store, thread_key, "codex")
    prov = CodexCliProvider(
        codex_bin=bin_name,
        dangerously_bypass_approvals_and_sandbox=bypass,
        model=model,
    )
    sid = store.get(thread_key, "codex")
    res = prov.run_turn(
        workspace=workspace, session_id=sid, prompt=prompt, on_progress=on_progress
    )
    if res.session_id and res.session_id != sid:
        store.upsert(thread_key, "codex", res.session_id, workspace=workspace)
    return res
