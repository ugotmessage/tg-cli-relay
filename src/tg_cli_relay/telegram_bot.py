from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from tg_cli_relay.attachments import (
    AttachmentConfig,
    DownloadedAttachment,
    build_attachment_prompt,
    cleanup_upload_dirs,
    download_attachment_to_path,
    extract_incoming_attachment,
    is_attachment_mime_confirmed,
    is_incoming_attachment_allowed,
    is_transient_telegram_error,
    parse_tgr_file_markers,
    prepare_download_path,
    resolve_outbound_files,
)
from tg_cli_relay.relay import effective_model_for_backend, relay_turn
from tg_cli_relay.task_context_resolver import might_refer_to_task, parse_resolution, resolution_prompt
from tg_cli_relay.providers.codex_app_server import steer_codex_turn
from tg_cli_relay.conversation_context import ConversationContext
from tg_cli_relay.session_store import Backend, SessionStore
from tg_cli_relay.task_registry import TaskRecord, TaskRegistry, make_task_title
from tg_cli_relay.task_router import (
    Route,
    format_router_status,
    format_router_test,
    load_task_router_config,
    route_text,
)
from tg_cli_relay.telegram_app import build_resilient_application
from tg_cli_relay.thread_key import TelegramIds, telegram_thread_key

log = logging.getLogger(__name__)

TG_CHUNK = 3800
_BOT_LOCK_FILE = None
_ACTIVE_RELAY_THREADS: set[str] = set()
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


@dataclass(slots=True)
class RelayJob:
    job_id: str
    parent_key: str
    worker_key: str
    backend: Backend
    started_at: float
    finished_at: float | None = None
    status: str = "starting"
    latest_progress: str = "等待 worker 啟動"
    title: str = ""


@dataclass(slots=True)
class QueuedTurn:
    msg: object
    key: str
    backend: Backend
    prompt: str
    store_path: Path
    attachment_config: AttachmentConfig
    route: Route
    task_id: str | None = None
    worker_key: str | None = None
    title: str = ""
    kind: str = "parent"


_ACTIVE_JOBS: dict[str, RelayJob] = {}
_RECENT_JOBS: dict[str, RelayJob] = {}
_RUNNING_TASKS: dict[str, RelayJob] = {}
_PENDING_TURNS: dict[str, deque[QueuedTurn]] = {}
_PENDING_TASK_TURNS: dict[str, deque[QueuedTurn]] = {}
_QUEUE_DRAIN_TASKS: dict[str, asyncio.Task[None]] = {}
_TASK_DRAIN_TASKS: dict[str, asyncio.Task[None]] = {}
SUPPORTED_BACKENDS: tuple[Backend, ...] = ("cursor", "codex", "claude", "opencode")


def _task_banner(job: RelayJob | TaskRecord | None) -> str:
    if job is None:
        return ""
    task_id = job.job_id if isinstance(job, RelayJob) else job.task_id
    title = (job.title or "").strip() or "untitled"
    return f"[task {task_id}] {title}"


def _with_task_banner(job: RelayJob | TaskRecord | None, text: str) -> str:
    banner = _task_banner(job)
    if not banner:
        return text
    body = text or ""
    if body.startswith(banner):
        return body
    return f"{banner}\n{body}" if body else banner


def _job_status_text(job: RelayJob) -> str:
    end = job.finished_at if job.finished_at is not None else time.monotonic()
    elapsed = max(0, int(end - job.started_at))
    latest = job.latest_progress.strip() or "尚無進度訊息"
    if len(latest) > 600:
        latest = latest[-600:]
    title = job.title.strip() or "untitled"
    return _with_task_banner(
        job,
        (
            f"背景工作 {job.job_id}\n"
            f"標題: {title}\n"
            f"狀態: {job.status}\n"
            f"已執行: {elapsed} 秒\n"
            f"最新進度: {latest}"
        ),
    )


def _task_status_text(task: TaskRecord, job: RelayJob | None = None) -> str:
    if job is not None and job.job_id == task.task_id:
        return _job_status_text(job)
    return _with_task_banner(
        task,
        (
            f"背景工作 {task.task_id}\n"
            f"標題: {task.title}\n"
            f"狀態: {task.status}\n"
            "最新進度: （程序重啟後僅保留登錄狀態，不會自動重跑）"
        ),
    )


def _format_task_list(tasks: list[TaskRecord]) -> str:
    if not tasks:
        return (
            "目前這個對話還沒有孤立任務。\n"
            "普通訊息仍走原本對話；新的背景工作會自動建立任務。\n"
            "用法: /task new <說明>\n"
            "用法: /task <id> [說明]"
        )
    lines = ["本對話的任務："]
    for task in tasks:
        lines.append(f"- {task.task_id} [{task.status}] {task.title}")
    lines.extend(
        [
            "",
            "回覆帶有 [task …] 的訊息可繼續該任務。",
            "用法: /task <id> [說明]",
            "用法: /task new <說明>",
        ]
    )
    return "\n".join(lines)


def _task_choice_text(tasks: list[TaskRecord]) -> str:
    lines = [
        "有多個任務可能符合，請回覆該任務的訊息或指定 /task <id>，不要直接當成舊的主對話。",
        "",
    ]
    lines.append(_format_task_list(tasks))
    return "\n".join(lines).strip()


def _unbound_reply_instructions(tasks: list[TaskRecord]) -> str:
    return (
        "這則訊息沒有綁定任務，無法判斷要繼續哪一項。"
        "請回覆帶有 [task …] 的訊息，或使用 /task <id>。\n\n"
        + _format_task_list(tasks)
    )


def _reply_to_message_id(msg) -> int | None:  # type: ignore[no-untyped-def]
    reply = getattr(msg, "reply_to_message", None)
    if reply is None:
        return None
    mid = getattr(reply, "message_id", None)
    return mid if isinstance(mid, int) else None


def _sent_message_id(sent) -> int | None:  # type: ignore[no-untyped-def]
    mid = getattr(sent, "message_id", None)
    return mid if isinstance(mid, int) else None


def _bind_sent_message(
    registry: TaskRegistry | None,
    job: RelayJob | TaskRecord | None,
    sent,
    role: str,
) -> None:
    if registry is None or job is None:
        return
    mid = _sent_message_id(sent)
    if mid is None:
        return
    task_id = job.job_id if isinstance(job, RelayJob) else job.task_id
    registry.bind_message(
        parent_key=job.parent_key,
        message_id=mid,
        task_id=task_id,
        backend=job.backend,
        role=role,
    )


def _bind_source_message(registry: TaskRegistry, task: TaskRecord, msg) -> None:  # type: ignore[no-untyped-def]
    mid = getattr(msg, "message_id", None)
    if not isinstance(mid, int):
        return
    registry.bind_message(
        parent_key=task.parent_key,
        message_id=mid,
        task_id=task.task_id,
        backend=task.backend,
        role="source",
    )


def _copy_parent_model_pref(store: SessionStore, parent_key: str, worker_key: str, backend: Backend) -> None:
    model = store.get_pref(parent_key, _model_pref_key(backend))
    if model:
        store.set_pref(worker_key, _model_pref_key(backend), model)


def _register_new_task(turn: QueuedTurn, store: SessionStore, registry: TaskRegistry, status: str) -> TaskRecord:
    task = registry.create_task(
        parent_key=turn.key,
        backend=turn.backend,
        title=make_task_title(turn.prompt),
        status=status,
    )
    _copy_parent_model_pref(store, turn.key, task.worker_key, turn.backend)
    ConversationContext(store.db_path).bind_task(turn.key, task.worker_key)
    _bind_source_message(registry, task, turn.msg)
    turn.task_id = task.task_id
    turn.worker_key = task.worker_key
    turn.title = task.title
    turn.kind = "new_task"
    return task


_PROGRESS_QUERY_RE = re.compile(
    r"^(?:"
    r"進度(?:如何|怎樣)?"
    r"|現在進度(?:如何|怎樣)?"
    r"|做到哪(?:裡)?(?:了)?"
    r"|現在怎樣|現在如何"
    r"|還在跑(?:嗎)?"
    r"|做好了嗎|好了嗎|做完了嗎|完成了嗎|跑完了嗎"
    r"|job\s*status"
    r"|progress"
    r")[\s?？!！.。]*$",
    re.IGNORECASE,
)
_CONTINUATION_RE = re.compile(
    r"^(?:"
    r"(?:請)?繼續(?:做|下去|吧)?"
    r"|接著(?:做|下去)?"
    r"|往下(?:做|去)?"
    r"|continue(?:\s+please)?"
    r"|keep\s+going"
    r"|go\s+on"
    r")[\s?？!！.。]*$",
    re.IGNORECASE,
)


_CONTEXTUAL_FOLLOWUP_RE = re.compile(
    r"^(?:剛剛|剛才|前面|上面|那個|這個|這部分|這一段|同一個|"
    r"再修|再改|再補|繼續|接著|往下|還沒|還有問題|還是不行|又出錯|改成|順便把)",
    re.IGNORECASE,
)


def _is_contextual_followup(text: str) -> bool:
    return bool(_CONTEXTUAL_FOLLOWUP_RE.match((text or "").strip()))


def _is_progress_query(text: str) -> bool:
    return bool(_PROGRESS_QUERY_RE.match((text or "").strip()))


def _is_bare_continuation(text: str) -> bool:
    return bool(_CONTINUATION_RE.match((text or "").strip()))


def _job_like_from_turn(turn: QueuedTurn) -> RelayJob | None:
    if not turn.task_id:
        return None
    return RelayJob(
        job_id=turn.task_id,
        parent_key=turn.key,
        worker_key=turn.worker_key or "",
        backend=turn.backend,
        started_at=time.monotonic(),
        title=turn.title,
    )


def _discard_queued_turn(pending: dict[str, deque[QueuedTurn]], key: str, turn: QueuedTurn) -> None:
    queue = pending.get(key)
    if queue is None:
        return
    try:
        queue.remove(turn)
    except ValueError:
        return
    if not queue:
        pending.pop(key, None)


def _pending_parent_turn_for_task(task_id: str) -> QueuedTurn | None:
    for queue in _PENDING_TURNS.values():
        for turn in queue:
            if turn.task_id == task_id:
                return turn
    return None


def _task_session_busy(task_id: str, worker_key: str | None) -> bool:
    if task_id in _RUNNING_TASKS:
        return True
    if worker_key and worker_key in _ACTIVE_RELAY_THREADS:
        return True
    return False


def _manual_resume_text(task: TaskRecord) -> str:
    return _with_task_banner(
        task,
        (
            f"任務 {task.task_id} 的原始請求尚未執行，不會自動跑後續訊息。\n"
            f"請回覆該任務的訊息，或使用 /task {task.task_id} <說明> 手動繼續。"
        ),
    )


async def _reply_task_status(
    msg,
    task: TaskRecord,
    registry: TaskRegistry,
    job: RelayJob | None = None,
) -> None:
    await _reply_text_with_retry(
        msg,
        _task_status_text(task, job),
        job=task,
        registry=registry,
        role="status",
    )


def _queue_limit() -> int:
    raw = os.environ.get("TGR_THREAD_QUEUE_MAX", "20").strip() or "20"
    return max(1, int(raw))


def _queue_size(thread_key: str) -> int:
    return len(_PENDING_TURNS.get(thread_key, ()))


def _allowed_ids() -> set[int] | None:
    raw = os.environ.get("TGR_ALLOWED_TELEGRAM_USER_IDS", "").strip()
    if not raw:
        return None
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        out.add(int(part))
    return out


def _parse_backend(raw: str) -> Backend | None:
    b = raw.strip().lower()
    if b in SUPPORTED_BACKENDS:
        return b  # type: ignore[return-value]
    return None


def _enabled_backends() -> tuple[Backend, ...]:
    raw = os.environ.get("TGR_ENABLED_BACKENDS", "").strip()
    if raw:
        backends = tuple(
            b for b in (_parse_backend(part) for part in raw.split(",")) if b is not None
        )
        if backends:
            return backends
    return SUPPORTED_BACKENDS


def _default_backend() -> Backend:
    backend = _parse_backend(os.environ.get("TGR_BACKEND", "cursor"))
    if backend is None:
        raise RuntimeError("TGR_BACKEND 必須是 cursor、codex、claude 或 opencode")
    return backend


def _backend_for_thread(store: SessionStore, thread_key: str) -> Backend:
    saved = store.get_pref(thread_key, "backend")
    backend = _parse_backend(saved or "")
    enabled = _enabled_backends()
    if backend in enabled:
        return backend
    default = _default_backend()
    return default if default in enabled else enabled[0]


def _model_pref_key(backend: Backend) -> str:
    return f"model:{backend}"


def _models_for_backend(backend: Backend) -> list[str]:
    if backend == "claude":
        from tg_cli_relay.providers.claude_cli import CLAUDE_MODELS

        return CLAUDE_MODELS
    if backend == "cursor":
        from tg_cli_relay.providers.cursor_agent import CURSOR_MODELS

        return CURSOR_MODELS
    if backend == "codex":
        from tg_cli_relay.providers.codex_cli import CODEX_MODELS

        return CODEX_MODELS
    if backend == "opencode":
        from tg_cli_relay.providers.opencode_cli import OPENCODE_MODELS

        return OPENCODE_MODELS
    return []


async def _models_for_backend_live(backend: Backend) -> list[str]:
    if backend == "cursor":
        from tg_cli_relay.providers.cursor_agent import list_cursor_models

        agent_bin = os.environ.get("TGR_CURSOR_AGENT_BIN", "agent").strip() or "agent"
        return await asyncio.to_thread(list_cursor_models, agent_bin)
    return _models_for_backend(backend)


async def _format_model_catalog(
    store: SessionStore, thread_key: str, active_backend: Backend
) -> str:
    lines = [f"目前 provider: {active_backend}", ""]
    for backend in _enabled_backends():
        marker = "*" if backend == active_backend else "-"
        current = effective_model_for_backend(store, thread_key, backend)
        lines.append(f"{marker} {backend} 目前模型: {current}")
        models = await _models_for_backend_live(backend)
        if models:
            lines.extend(f"  {model}" for model in models)
        else:
            lines.append("  (未提供模型清單，可直接輸入 model id)")
        lines.append("")
    lines.append("用法: /provider <provider>")
    lines.append("用法: /model <model>")
    lines.append("用法: /model <provider> <model>")
    return "\n".join(lines).strip()


def _get_store() -> SessionStore:
    store_path = Path(os.environ.get("TGR_SESSION_DB", str(Path("data") / "sessions.sqlite3")))
    return SessionStore(store_path)


def _get_thread_key(update) -> str:  # type: ignore[no-untyped-def]
    chat = update.effective_chat
    msg = update.message
    thread_id = msg.message_thread_id if msg else None
    return telegram_thread_key(chat_type=str(chat.type), ids=TelegramIds(chat.id, thread_id))


def _check_auth(update) -> bool:  # type: ignore[no-untyped-def]
    if update.effective_user is None:
        return False
    allow = _allowed_ids()
    return allow is None or update.effective_user.id in allow


async def _cmd_reset(update, context) -> None:  # type: ignore[no-untyped-def]
    if not _check_auth(update):
        return
    key = _get_thread_key(update)
    if key in _ACTIVE_RELAY_THREADS or _PENDING_TURNS.get(key):
        await update.message.reply_text("主對話仍在執行或排隊，請完成後再重置。")
        return
    store = _get_store()
    backend = _backend_for_thread(store, key)
    store.delete(key, backend)
    TaskRegistry(store.db_path).clear_focus(key)
    ConversationContext(store.db_path).reset(key)
    await update.message.reply_text(f"對話已重置，下一則訊息將開啟新的 {backend} session。")


async def _cmd_status(update, context) -> None:  # type: ignore[no-untyped-def]
    if not _check_auth(update):
        return
    key = _get_thread_key(update)
    store = _get_store()
    backend = _backend_for_thread(store, key)
    sid = store.get(key, backend)
    ws = os.environ.get("TGR_DEFAULT_WORKSPACE", "(未設定)")
    lines = [
        f"Provider: {backend}",
        f"工作目錄: {ws}",
        f"Session: {(sid[:8] + '...') if sid else '（尚未建立）'}",
        f"模型: {effective_model_for_backend(store, key, backend)}",
        f"可用 providers: {', '.join(_enabled_backends())}",
    ]
    job = _ACTIVE_JOBS.get(key) or _RECENT_JOBS.get(key)
    if job is not None:
        lines.extend(["", _job_status_text(job)])
    registry = TaskRegistry(store.db_path)
    tasks = registry.list_tasks(key)
    if tasks:
        lines.extend(["", _format_task_list(tasks)])
    queued = _queue_size(key)
    if queued:
        lines.extend(["", f"等待中的訊息: {queued}"])
    await update.message.reply_text("\n".join(lines))


async def _cmd_provider(update, context) -> None:  # type: ignore[no-untyped-def]
    if not _check_auth(update):
        return

    key = _get_thread_key(update)
    store = _get_store()
    args: list[str] = context.args or []
    enabled = _enabled_backends()
    current = _backend_for_thread(store, key)

    if not args:
        lines = [f"目前 provider: {current}", "", "可用 providers:"]
        lines.extend(f"  {backend}" for backend in enabled)
        lines.append("")
        lines.append("用法: /provider <provider>")
        await update.message.reply_text("\n".join(lines))
        return

    chosen = _parse_backend(args[0])
    if chosen is None or chosen not in enabled:
        await update.message.reply_text(
            f"不支援的 provider: {args[0]}\n可用: {', '.join(enabled)}"
        )
        return

    store.set_pref(key, "backend", chosen)
    await update.message.reply_text(f"Provider 已切換至 {chosen}（下一輪起生效）。")


async def _cmd_model(update, context) -> None:  # type: ignore[no-untyped-def]
    if not _check_auth(update):
        return

    key = _get_thread_key(update)
    store = _get_store()
    backend = _backend_for_thread(store, key)
    args: list[str] = context.args or []

    if not args:
        catalog = await _format_model_catalog(store, key, backend)
        for chunk in _chunk_reply(catalog):
            await update.message.reply_text(chunk)
        return

    maybe_backend = _parse_backend(args[0])
    if maybe_backend is not None:
        if maybe_backend not in _enabled_backends():
            await update.message.reply_text(
                f"不支援的 provider: {args[0]}\n可用: {', '.join(_enabled_backends())}"
            )
            return
        backend = maybe_backend
        store.set_pref(key, "backend", backend)
        if len(args) == 1:
            current = effective_model_for_backend(store, key, backend)
            models = "\n".join(f"  {m}" for m in await _models_for_backend_live(backend))
            body = f"Provider 已切換至 {backend}\n目前模型: {current}\n\n可用模型:\n{models}"
            for chunk in _chunk_reply(body):
                await update.message.reply_text(chunk)
            return
        chosen = args[1]
    else:
        chosen = args[0]

    models = await _models_for_backend_live(backend)
    if models and chosen not in models:
        await update.message.reply_text(f"不支援的模型: {chosen}\n可用: {', '.join(models)}")
        return

    store.set_pref(key, _model_pref_key(backend), chosen)
    await update.message.reply_text(f"{backend} 模型已切換至 {chosen}（下一輪起生效）。")


async def _cmd_router(update, context) -> None:  # type: ignore[no-untyped-def]
    if not _check_auth(update):
        return
    args: list[str] = context.args or []
    if args and args[0].lower() == "test":
        text = " ".join(args[1:]).strip()
        if not text:
            await update.message.reply_text("用法: /router test <文字>")
            return
        await update.message.reply_text(format_router_test(text))
        return
    await update.message.reply_text(format_router_status())


async def _cmd_jobs(update, context) -> None:  # type: ignore[no-untyped-def]
    """Legacy alias: show new tasks plus prior JobStore jobs, if present."""
    if not _check_auth(update):
        return
    key = _get_thread_key(update)
    db = _get_store().db_path
    lines = [_format_task_list(TaskRegistry(db).list_tasks(key))]
    try:
        from tg_cli_relay.job_store import JobStore
        legacy = JobStore(db).list_for_thread(key, limit=5)
        if legacy:
            lines.append("舊版背景 Jobs：")
            lines.extend(f"#{j.id} [{j.status}] {j.backend}" for j in legacy)
    except ImportError:
        pass
    await update.message.reply_text("\n\n".join(lines))


async def _cmd_job(update, context) -> None:  # type: ignore[no-untyped-def]
    """Accept task IDs and retained numeric JobStore IDs."""
    if not _check_auth(update):
        return
    args = list(context.args or [])
    if not args:
        await update.message.reply_text("用法: /job <id>（新版任務亦可用 /task <id>）")
        return
    key = _get_thread_key(update)
    db = _get_store().db_path
    task = TaskRegistry(db).get_task(args[0])
    if task is not None and task.parent_key == key:
        await _reply_task_status(update.message, task, TaskRegistry(db))
        return
    if args[0].isdigit():
        try:
            from tg_cli_relay.job_store import JobStore
            job = JobStore(db).get(int(args[0]))
        except ImportError:
            job = None
        if job is not None and job.thread_key == key:
            body = f"Job #{job.id}\n狀態: {job.status}\nProvider: {job.backend}"
            if job.result_preview:
                body += "\n結果預覽: " + job.result_preview[:1200]
            if job.error:
                body += "\n錯誤: " + job.error
            await update.message.reply_text(body)
            return
    await update.message.reply_text("找不到此對話的 Job。")


async def _cmd_task(update, context) -> None:  # type: ignore[no-untyped-def]
    if not _check_auth(update):
        return
    msg = update.message
    if msg is None:
        return
    key = _get_thread_key(update)
    store = _get_store()
    registry = TaskRegistry(store.db_path)
    args: list[str] = list(context.args or [])
    if not args:
        await msg.reply_text(_format_task_list(registry.list_tasks(key)))
        return
    if args[0].lower() == "clear":
        registry.clear_focus(key)
        await msg.reply_text("已清除目前任務焦點。")
        return
    if args[0].lower() == "active":
        task = registry.focused_task(key)
        await msg.reply_text(_task_status_text(task) if task else "目前沒有選定任務。")
        return
    if args[0].lower() == "use":
        task_id = args[1] if len(args) > 1 else ""
        if not task_id or not registry.focus_task(key, task_id):
            await msg.reply_text("用法: /task use <本對話任務ID>")
            return
        task = registry.focused_task(key)
        assert task is not None
        await msg.reply_text(f"目前任務已切換至 [{task.task_id}] {task.title}。")
        return
    if args[0].lower() == "new":
        prompt = " ".join(args[1:]).strip()
        if not prompt:
            await msg.reply_text("用法: /task new <說明>")
            return
        attachment_config = _attachment_config()
        store_path = store.db_path
        backend = _backend_for_thread(store, key)
        decision = route_text(prompt, load_task_router_config().config)
        turn = QueuedTurn(
            msg=msg,
            key=key,
            backend=backend,
            prompt=prompt,
            store_path=store_path,
            attachment_config=attachment_config,
            route=decision.route,
            kind="new_task",
        )
        busy = key in _ACTIVE_RELAY_THREADS or key in _ACTIVE_JOBS or bool(_PENDING_TURNS.get(key))
        _register_new_task(turn, store, registry, "queued" if busy else "starting")
        if busy:
            queued_ok = await _enqueue_turn(turn)
            if not queued_ok:
                registry.update_status(turn.task_id, "failed")
            return
        await _start_background_job(turn)
        return

    task_id = args[0]
    prompt = " ".join(args[1:]).strip()
    task = registry.get_task(task_id)
    if task is None:
        await msg.reply_text(f"找不到任務 {task_id}。請用 /task 查看本對話的任務。")
        return
    if task.parent_key != key:
        await msg.reply_text(
            f"任務 {task_id} [{task.title}] 不屬於這個對話／主題，無法在此繼續。"
        )
        return
    registry.focus_task(key, task.task_id)
    if not prompt:
        job = _RUNNING_TASKS.get(task.task_id)
        await _reply_task_status(msg, task, registry, job)
        return
    await _resume_existing_task(
        task,
        msg=msg,
        prompt=prompt,
        store_path=store.db_path,
        attachment_config=_attachment_config(),
        registry=registry,
    )


async def _cmd_help(update, context) -> None:  # type: ignore[no-untyped-def]
    if not _check_auth(update):
        return
    cfg = _attachment_config()
    lines = [
        "/reset — 清除對話，開啟新 session",
        "/status — 查看目前後端與 session 狀態",
        "/provider [名稱] — 查看或切換 CLI provider",
        "/model [provider] [名稱] — 查看或切換模型",
        "/router — 查看 Task Router 規則與設定來源",
        "/router test <文字> — 測試 DIRECT / DELEGATE 判斷",
        "/task — 列出本對話的孤立任務",
        "/task <id> [說明] — 查看或繼續指定任務",
        "/task new <說明> — 開新的孤立任務（含小型 DIRECT）",
        "進度 — 查詢目前背景 worker 狀態",
        "忙碌期間的新訊息會依序排隊，完成後自動處理。",
        "/help — 顯示此說明",
        "",
        "附件：可傳 document / photo / audio / voice / video（含 caption）。",
        f"下載上限：{cfg.max_download_bytes} bytes；回傳上限：{cfg.max_upload_bytes} bytes。",
        "Agent 以獨立一行 TGR_FILE:/abs/path 宣告回傳檔案（最多 "
        f"{cfg.max_files_per_message} 個）。",
        f"上傳暫存保留 {cfg.upload_retention_hours} 小時後可清理。",
    ]
    await update.message.reply_text("\n".join(lines))


def _chunk_reply(text: str) -> list[str]:
    t = text or ""
    soft_limit = min(TG_CHUNK, int(os.environ.get("TGR_REPLY_SOFT_CHUNK", "1400")))
    if len(t) <= soft_limit:
        return [t] if t else []
    chunks: list[str] = []
    current = ""
    blocks = re.split(r"\n{2,}", t)
    for block in (part.strip() for part in blocks):
        if not block:
            continue
        if re.match(r"^#{1,6}\s", block) and current:
            chunks.append(current)
            current = ""
        if len(block) > soft_limit:
            lines = block.splitlines() or [block]
        else:
            lines = [block]
        for line in lines:
            pieces = [line[i : i + soft_limit] for i in range(0, len(line), soft_limit)] or [""]
            for piece in pieces:
                candidate = f"{current}\n\n{piece}".strip() if current else piece
                if current and len(candidate) > soft_limit:
                    chunks.append(current)
                    current = piece
                else:
                    current = candidate
    if current:
        chunks.append(current)
    return chunks


def _chunk_segments(segments: list[str]) -> list[str]:
    """依邏輯分段 chunk；每段獨立遵守 TG 字數上限，段與段之間不跨 chunk 合併。"""
    chunks: list[str] = []
    for segment in segments:
        chunks.extend(_chunk_reply(segment))
    return chunks


def _is_transient_telegram_error(exc: Exception) -> bool:
    return is_transient_telegram_error(exc)


async def _reply_text_with_retry(  # type: ignore[no-untyped-def]
    message,
    text: str,
    retries: int = 2,
    delay_seconds: float = 2.0,
    *,
    job: RelayJob | TaskRecord | None = None,
    registry: TaskRegistry | None = None,
    role: str = "progress",
    parent_key: str | None = None,
    parent_backend: Backend | None = None,
):
    attempt = 0
    while True:
        try:
            sent = await message.reply_text(text)
            _bind_sent_message(registry, job, sent, role)
            mid = _sent_message_id(sent)
            if registry is not None and job is None and parent_key and parent_backend and mid is not None:
                registry.bind_parent_message(
                    parent_key=parent_key, message_id=mid, backend=parent_backend
                )
            return sent
        except Exception as exc:
            if attempt >= retries or not _is_transient_telegram_error(exc):
                raise
            attempt += 1
            log.warning("reply_text 暫時失敗，%s 秒後重試 (%s/%s): %s", delay_seconds, attempt, retries, exc)
            await asyncio.sleep(delay_seconds)


async def _reply_document_with_retry(  # type: ignore[no-untyped-def]
    message,
    path: Path,
    retries: int = 2,
    delay_seconds: float = 2.0,
    *,
    job: RelayJob | TaskRecord | None = None,
    registry: TaskRegistry | None = None,
    role: str = "attachment",
    parent_key: str | None = None,
    parent_backend: Backend | None = None,
):
    attempt = 0
    while True:
        try:
            with path.open("rb") as fh:
                sent = await message.reply_document(document=fh, filename=path.name)
            _bind_sent_message(registry, job, sent, role)
            mid = _sent_message_id(sent)
            if registry is not None and job is None and parent_key and parent_backend and mid is not None:
                registry.bind_parent_message(
                    parent_key=parent_key, message_id=mid, backend=parent_backend
                )
            return sent
        except Exception as exc:
            if attempt >= retries or not _is_transient_telegram_error(exc):
                raise
            attempt += 1
            log.warning(
                "reply_document 暫時失敗，%s 秒後重試 (%s/%s): %s",
                delay_seconds,
                attempt,
                retries,
                exc,
            )
            await asyncio.sleep(delay_seconds)


def _bot_fingerprint_from_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _attachment_config() -> AttachmentConfig:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    fp = _bot_fingerprint_from_token(token) if token else "default"
    return AttachmentConfig.from_env(bot_fingerprint=fp)


def _supported_types_hint() -> str:
    return (
        "請傳送純文字，或支援的附件（document、photo、audio、voice、video）。"
        "附件可附 caption 作為處理說明。"
    )


async def _download_incoming_attachment(
    msg,
    attachment_config: AttachmentConfig,
) -> tuple[DownloadedAttachment | None, str | None]:
    """Download one attachment; returns (result, user_error)."""
    incoming = extract_incoming_attachment(msg)
    if incoming is None:
        return None, None

    if incoming.file_size is not None and incoming.file_size > attachment_config.max_download_bytes:
        return None, (
            f"附件大小（{incoming.file_size} bytes）超過上限 "
            f"（{attachment_config.max_download_bytes} bytes）。"
        )

    if incoming.mime_type and not is_incoming_attachment_allowed(
        incoming.mime_type,
        incoming.original_name,
        attachment_config.allowed_download_mime_types,
    ):
        return None, "不支援的附件類型。"

    chat_id = msg.chat_id
    message_id = msg.message_id
    existing: set[str] = set()
    try:
        dest = prepare_download_path(attachment_config, chat_id, message_id, incoming, existing)
    except ValueError as exc:
        log.warning("prepare_download_path 失敗: %s", exc)
        return None, "無法建立安全的本機儲存路徑。"

    try:
        size = await download_attachment_to_path(
            msg.get_bot(),
            incoming,
            dest,
            max_bytes=attachment_config.max_download_bytes,
        )
    except ValueError as exc:
        if "超過上限" in str(exc):
            return None, str(exc)
        log.exception("附件下載大小驗證失敗")
        return None, "附件下載失敗（大小驗證）。"
    except Exception:
        log.exception("Telegram 附件下載失敗 chat=%s msg=%s", chat_id, message_id)
        return None, "附件下載暫時失敗，請稍後再試。"

    mime_confirmed = is_attachment_mime_confirmed(
        incoming.mime_type,
        incoming.original_name,
        attachment_config.allowed_download_mime_types,
    )
    return DownloadedAttachment(
        local_path=dest,
        original_name=incoming.original_name or dest.name,
        mime_type=incoming.mime_type,
        mime_confirmed=mime_confirmed,
        size_bytes=size,
        caption=msg.caption,
    ), None


async def _send_relay_output(
    msg,
    body: str,
    *,
    backend: Backend,
    model: str | None,
    sid: str | None,
    attachment_config: AttachmentConfig,
    segments: list[str] | None = None,
    job: RelayJob | None = None,
    registry: TaskRegistry | None = None,
    parent_key: str | None = None,
) -> None:
    parsed = parse_tgr_file_markers(body)
    upload_paths, upload_errors = resolve_outbound_files(
        parsed.marker_paths,
        attachment_config,
        existing_errors=parsed.marker_errors,
    )

    footer_parts: list[str] = [backend]
    if model:
        footer_parts.append(model)
    if sid:
        footer_parts.append(f"session:{sid}")
    footer = "\n\n— " + " · ".join(footer_parts) if footer_parts else ""

    if segments and len(segments) > 1:
        display_segments = [
            parse_tgr_file_markers(segment).display_text for segment in segments
        ]
        chunks = _chunk_segments(display_segments)
    else:
        text_body = parsed.display_text
        chunks = _chunk_reply(text_body)

    if upload_errors:
        err_block = "\n".join(upload_errors)
        if chunks:
            chunks[-1] = f"{chunks[-1]}\n\n{err_block}".strip()
        else:
            chunks = [err_block]

    if not chunks and not upload_paths:
        chunks = ["(無輸出)"]

    for i, part in enumerate(chunks):
        role = "final" if i == len(chunks) - 1 and not upload_paths else "final"
        await _reply_text_with_retry(
            msg,
            _with_task_banner(job, part + (footer if i == len(chunks) - 1 else "")),
            job=job,
            registry=registry,
            role=role,
            parent_key=parent_key,
            parent_backend=backend if parent_key else None,
        )

    upload_failures: list[str] = []
    for path in upload_paths:
        try:
            await _reply_document_with_retry(
                msg, path, job=job, registry=registry, role="attachment",
                parent_key=parent_key, parent_backend=backend if parent_key else None,
            )
        except Exception:
            log.exception("Telegram 附件上傳失敗 path=%s", path.name)
            upload_failures.append(f"無法傳送附件：{path.name}")

    if upload_failures:
        await _reply_text_with_retry(
            msg,
            _with_task_banner(job, "\n".join(upload_failures)),
            job=job,
            registry=registry,
            role="error",
            parent_key=parent_key,
            parent_backend=backend if parent_key else None,
        )


async def _execute_relay_turn(
    *,
    msg,
    key: str,
    backend: Backend,
    prompt: str,
    store: SessionStore,
    attachment_config: AttachmentConfig,
    show_typing: bool,
    job: RelayJob | None = None,
) -> None:
    typing_task: asyncio.Task[None] | None = None
    try:
        from telegram.constants import ChatAction

        async def _typing_loop() -> None:
            while True:
                try:
                    await msg.chat.send_action(action=ChatAction.TYPING)
                except Exception as exc:
                    log.warning(
                        "Telegram typing action failed; continuing without typing indicator: %s",
                        exc,
                    )
                await asyncio.sleep(4)

        if show_typing:
            typing_task = asyncio.create_task(_typing_loop())

        loop = asyncio.get_running_loop()
        registry = TaskRegistry(store.db_path)

        def _live_progress(text: str) -> None:
            if job is not None:
                job.status = "running"
                job.latest_progress = text
                if registry is not None:
                    registry.update_status(job.job_id, "running")
            future = asyncio.run_coroutine_threadsafe(
                _reply_text_with_retry(
                    msg,
                    _with_task_banner(job, text),
                    job=job,
                    registry=registry,
                    role="progress",
                ),
                loop,
            )
            try:
                future.result()
            except Exception as exc:
                log.warning("Codex live progress delivery failed: %s", exc)

        try:
            res = await asyncio.to_thread(
                relay_turn,
                thread_key=key,
                backend=backend,
                prompt=prompt,
                store=store,
                on_progress=_live_progress if backend == "codex" else None,
            )
        except Exception as exc:
            log.exception("relay_turn 失敗 thread=%s", key)
            if job is not None:
                job.status = "failed"
                job.latest_progress = f"relay 執行失敗: {type(exc).__name__}"
                if registry is not None:
                    registry.update_status(job.job_id, "failed")
            await _reply_text_with_retry(
                msg,
                _with_task_banner(job, "執行失敗，請查看伺服器日誌。"),
                job=job,
                registry=registry,
                role="error",
                parent_key=key if job is None else None,
                parent_backend=backend if job is None else None,
            )
            return

        out = res.stdout or ""
        err = res.stderr or ""
        if res.returncode == 0:
            body = out
            if job is not None:
                job.status = "completed"
                job.latest_progress = "worker 已完成並回傳結果"
                if registry is not None:
                    registry.update_status(job.job_id, "completed")
        elif err.strip():
            body = f"{out}\n\n--- stderr ---\n{err}".strip()
            if job is not None:
                job.status = "failed"
                job.latest_progress = f"worker 結束，return code {res.returncode}"
                if registry is not None:
                    registry.update_status(job.job_id, "failed")
        else:
            body = out
            if job is not None:
                job.status = "failed"
                job.latest_progress = f"worker 結束，return code {res.returncode}"
                if registry is not None:
                    registry.update_status(job.job_id, "failed")

        sid = store.get(key, backend)
        model = effective_model_for_backend(store, key, backend)
        await _send_relay_output(
            msg,
            body,
            backend=backend,
            model=model,
            sid=sid,
            attachment_config=attachment_config,
            segments=res.stdout_segments,
            job=job,
            registry=registry,
            parent_key=key if job is None else None,
        )
    finally:
        _ACTIVE_RELAY_THREADS.discard(key)
        if typing_task is not None:
            typing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await typing_task


def _job_from_turn(turn: QueuedTurn, *, occupy_parent: bool) -> RelayJob:
    if not turn.task_id or not turn.worker_key:
        raise RuntimeError("task routing must be frozen before execution")
    job = RelayJob(
        job_id=turn.task_id,
        parent_key=turn.key,
        worker_key=turn.worker_key,
        backend=turn.backend,
        started_at=time.monotonic(),
        title=turn.title,
        status="starting",
    )
    if occupy_parent:
        _ACTIVE_JOBS[turn.key] = job
    _RUNNING_TASKS[job.job_id] = job
    TaskRegistry(turn.store_path).update_status(job.job_id, "running")
    return job


async def _run_background_job(
    *,
    msg,
    job: RelayJob,
    prompt: str,
    store_path: Path,
    attachment_config: AttachmentConfig,
    occupy_parent: bool,
) -> None:
    registry = TaskRegistry(store_path)
    try:
        job.status = "running"
        job.latest_progress = "Orchestrator 已啟動獨立 worker session"
        registry.update_status(job.job_id, "running")
        await _execute_relay_turn(
            msg=msg,
            key=job.worker_key,
            backend=job.backend,
            prompt=prompt,
            store=SessionStore(store_path),
            attachment_config=attachment_config,
            show_typing=False,
            job=job,
        )
    except Exception as exc:
        job.status = "failed"
        job.latest_progress = f"背景工作異常中止: {type(exc).__name__}"
        registry.update_status(job.job_id, "failed")
        log.exception("背景 relay job 失敗 job=%s", job.job_id)
    finally:
        job.finished_at = time.monotonic()
        _RUNNING_TASKS.pop(job.job_id, None)
        current = _ACTIVE_JOBS.get(job.parent_key)
        if occupy_parent and current is not None and current.job_id == job.job_id:
            _ACTIVE_JOBS.pop(job.parent_key, None)
            _RECENT_JOBS[job.parent_key] = job
            _schedule_queue_drain(job.parent_key)
        _schedule_task_drain(job.job_id)


async def _start_background_job(turn: QueuedTurn) -> None:
    if turn.task_id and _task_session_busy(turn.task_id, turn.worker_key):
        _PENDING_TURNS.setdefault(turn.key, deque()).appendleft(turn)
        return
    registry = TaskRegistry(turn.store_path)
    job = _job_from_turn(turn, occupy_parent=True)
    try:
        await _reply_text_with_retry(
            turn.msg,
            _with_task_banner(
                job,
                f"已派出背景 worker（job {job.job_id}）。主對話可繼續使用；傳送「進度」可隨時查詢，完成後會自動回報。",
            ),
            job=job,
            registry=registry,
            role="ack",
        )
    except Exception:
        _ACTIVE_JOBS.pop(turn.key, None)
        _RUNNING_TASKS.pop(job.job_id, None)
        registry.update_status(job.job_id, "failed")
        _PENDING_TASK_TURNS.pop(job.job_id, None)
        _schedule_queue_drain(turn.key)
        raise
    task = asyncio.create_task(
        _run_background_job(
            msg=turn.msg,
            job=job,
            prompt=turn.prompt,
            store_path=turn.store_path,
            attachment_config=turn.attachment_config,
            occupy_parent=True,
        )
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def _start_task_followup(turn: QueuedTurn) -> None:
    if not turn.task_id:
        raise RuntimeError("follow-up routing must be frozen at arrival")
    if _task_session_busy(turn.task_id, turn.worker_key) or _pending_parent_turn_for_task(turn.task_id):
        _PENDING_TASK_TURNS.setdefault(turn.task_id, deque()).appendleft(turn)
        return
    registry = TaskRegistry(turn.store_path)
    job = _job_from_turn(turn, occupy_parent=False)
    try:
        await _reply_text_with_retry(
            turn.msg,
            _with_task_banner(job, f"已傳送到任務 {job.job_id}，完成後會回覆同一任務。"),
            job=job,
            registry=registry,
            role="ack",
        )
    except Exception:
        _RUNNING_TASKS.pop(job.job_id, None)
        registry.update_status(job.job_id, "failed")
        _schedule_task_drain(job.job_id)
        raise
    task = asyncio.create_task(
        _run_background_job(
            msg=turn.msg,
            job=job,
            prompt=turn.prompt,
            store_path=turn.store_path,
            attachment_config=turn.attachment_config,
            occupy_parent=False,
        )
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def _enqueue_task_followup(turn: QueuedTurn) -> bool:
    if not turn.task_id:
        raise RuntimeError("follow-up routing must be frozen at arrival")
    queue = _PENDING_TASK_TURNS.setdefault(turn.task_id, deque())
    job_like = _job_like_from_turn(turn)
    registry = TaskRegistry(turn.store_path)
    if len(queue) >= _queue_limit():
        await _reply_text_with_retry(
            turn.msg,
            _with_task_banner(
                job_like,
                f"任務 {turn.task_id} 的等待佇列已滿（上限 {_queue_limit()} 則），請稍後再傳。",
            ),
            job=job_like,
            registry=registry,
            role="error",
        )
        return False
    queue.append(turn)
    try:
        await _reply_text_with_retry(
            turn.msg,
            _with_task_banner(
                job_like,
                f"已加入任務 {turn.task_id} 的等待佇列，目前順位 {len(queue)}；前一項完成後會自動處理並回覆。",
            ),
            job=job_like,
            registry=registry,
            role="ack",
        )
    except Exception:
        _discard_queued_turn(_PENDING_TASK_TURNS, turn.task_id, turn)
        _schedule_task_drain(turn.task_id)
        raise
    _schedule_task_drain(turn.task_id)
    return True


async def _drain_task_queue(task_id: str) -> None:
    try:
        while True:
            queue = _PENDING_TASK_TURNS.get(task_id)
            if not queue:
                _PENDING_TASK_TURNS.pop(task_id, None)
                return
            turn = queue[0]
            if _task_session_busy(task_id, turn.worker_key) or _pending_parent_turn_for_task(task_id):
                return
            record = TaskRegistry(turn.store_path).get_task(task_id)
            if record is not None and record.status in ("queued", "starting") and task_id not in _RUNNING_TASKS:
                return
            queue.popleft()
            await _start_task_followup(turn)
    finally:
        _TASK_DRAIN_TASKS.pop(task_id, None)


def _schedule_task_drain(task_id: str) -> None:
    current = _TASK_DRAIN_TASKS.get(task_id)
    if current is not None and not current.done():
        return
    task = asyncio.create_task(_drain_task_queue(task_id))
    _TASK_DRAIN_TASKS[task_id] = task
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def _resume_existing_task(
    task: TaskRecord,
    *,
    msg,
    prompt: str,
    store_path: Path,
    attachment_config: AttachmentConfig,
    registry: TaskRegistry,
) -> None:
    registry.focus_task(task.parent_key, task.task_id)
    _bind_source_message(registry, task, msg)
    turn = QueuedTurn(
        msg=msg,
        key=task.parent_key,
        backend=task.backend,  # type: ignore[arg-type]
        prompt=prompt,
        store_path=store_path,
        attachment_config=attachment_config,
        route=Route.DIRECT,
        task_id=task.task_id,
        worker_key=task.worker_key,
        title=task.title,
        kind="task_followup",
    )
    initial_pending = _pending_parent_turn_for_task(task.task_id) is not None
    session_busy = _task_session_busy(task.task_id, task.worker_key)
    if task.status in ("queued", "starting") and not session_busy:
        if initial_pending or task.status == "starting":
            await _enqueue_task_followup(turn)
            return
        await _reply_text_with_retry(
            msg,
            _manual_resume_text(task),
            job=task,
            registry=registry,
            role="error",
        )
        return
    if session_busy or initial_pending or _PENDING_TASK_TURNS.get(task.task_id):
        await _enqueue_task_followup(turn)
        return
    await _start_task_followup(turn)


def _task_context_enabled() -> bool:
    return os.environ.get("TGR_TASK_CONTEXT_RESOLVER", "1").strip().lower() not in (
        "0", "false", "off", "no"
    )


async def _run_context_resolution(turn: QueuedTurn) -> None:
    """Ask the same primary CLI for a routing decision; Relay alone dispatches it."""
    _ACTIVE_RELAY_THREADS.add(turn.key)
    registry = TaskRegistry(turn.store_path)
    try:
        tasks = registry.list_tasks(turn.key, limit=8)
        if not tasks:
            # Jobs may have been deleted while queued; do not guess a target.
            decision = None
        else:
            try:
                result = await asyncio.to_thread(
                    relay_turn,
                    thread_key=turn.key, backend=turn.backend,
                    prompt=resolution_prompt(turn.prompt, tasks),
                    store=SessionStore(turn.store_path),
                    routing_only=True,
                )
                decision = parse_resolution(result.stdout, tasks) if result.returncode == 0 else None
            except Exception as exc:
                log.warning("Primary CLI task routing failed, require explicit choice: %s", exc)
                decision = None

        if decision is not None and decision.action == "task" and decision.task_id:
            task = registry.get_task(decision.task_id)
            # IDs returned by an LLM are always untrusted: enforce chat/topic ownership.
            if task is None or task.parent_key != turn.key:
                decision = None
            else:
                if turn.kind == "resolve_status":
                    registry.focus_task(turn.key, task.task_id)
                    _bind_source_message(registry, task, turn.msg)
                    await _reply_task_status(
                        turn.msg, task, registry, _RUNNING_TASKS.get(task.task_id)
                    )
                else:
                    await _resume_existing_task(
                        task, msg=turn.msg, prompt=turn.prompt,
                        store_path=turn.store_path,
                        attachment_config=turn.attachment_config, registry=registry,
                    )
                return

        if not tasks or (decision is not None and decision.action == "new"):
            # A new request continues through the regular router, not a forced
            # old task. Preserve the user's exact text, not the routing prompt.
            route = route_text(turn.prompt, load_task_router_config().config).route
            if route == Route.DELEGATE:
                next_turn = QueuedTurn(
                    msg=turn.msg, key=turn.key, backend=turn.backend,
                    prompt=turn.prompt, store_path=turn.store_path,
                    attachment_config=turn.attachment_config, route=route,
                )
                store = SessionStore(turn.store_path)
                _register_new_task(next_turn, store, registry, "queued")
                queued = await _enqueue_turn(next_turn)
                if not queued:
                    registry.update_status(next_turn.task_id, "failed")
            else:
                mid = getattr(turn.msg, "message_id", None)
                if isinstance(mid, int):
                    registry.bind_parent_message(
                        parent_key=turn.key, message_id=mid, backend=turn.backend
                    )
                await _execute_relay_turn(
                    msg=turn.msg, key=turn.key, backend=turn.backend,
                    prompt=turn.prompt, store=SessionStore(turn.store_path),
                    attachment_config=turn.attachment_config, show_typing=True,
                )
            return

        # Malformed output, provider error, or genuine uncertainty: fail
        # closed and let the human select. Never execute a guessed task.
        await _reply_text_with_retry(turn.msg, _task_choice_text(tasks))
    finally:
        _ACTIVE_RELAY_THREADS.discard(turn.key)
        _schedule_queue_drain(turn.key)


async def _run_direct_turn(turn: QueuedTurn) -> None:
    if turn.kind in ("resolve_task", "resolve_status"):
        await _run_context_resolution(turn)
        return
    # Bind source at execution time even if this turn was previously queued.
    mid = getattr(turn.msg, "message_id", None)
    if isinstance(mid, int):
        TaskRegistry(turn.store_path).bind_parent_message(
            parent_key=turn.key, message_id=mid, backend=turn.backend
        )
    _ACTIVE_RELAY_THREADS.add(turn.key)
    await _execute_relay_turn(
        msg=turn.msg,
        key=turn.key,
        backend=turn.backend,
        prompt=turn.prompt,
        store=SessionStore(turn.store_path),
        attachment_config=turn.attachment_config,
        show_typing=True,
    )
    _schedule_queue_drain(turn.key)


async def _drain_thread_queue(key: str) -> None:
    try:
        while True:
            queue = _PENDING_TURNS.get(key)
            if not queue:
                _PENDING_TURNS.pop(key, None)
                return
            turn = queue[0]
            if key in _ACTIVE_RELAY_THREADS:
                return
            if turn.kind == "new_task" or turn.route == Route.DELEGATE:
                if key in _ACTIVE_JOBS:
                    return
                if turn.task_id and _task_session_busy(turn.task_id, turn.worker_key):
                    return
                queue.popleft()
                if not turn.task_id or not turn.worker_key:
                    store = SessionStore(turn.store_path)
                    _register_new_task(turn, store, TaskRegistry(turn.store_path), "starting")
                await _start_background_job(turn)
                continue
            queue.popleft()
            await _run_direct_turn(turn)
    finally:
        _QUEUE_DRAIN_TASKS.pop(key, None)


def _schedule_queue_drain(key: str) -> None:
    current = _QUEUE_DRAIN_TASKS.get(key)
    if current is not None and not current.done():
        return
    task = asyncio.create_task(_drain_thread_queue(key))
    _QUEUE_DRAIN_TASKS[key] = task
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def _enqueue_turn(turn: QueuedTurn) -> bool:
    queue = _PENDING_TURNS.setdefault(turn.key, deque())
    job = _job_like_from_turn(turn)
    registry = TaskRegistry(turn.store_path) if turn.task_id else None
    if len(queue) >= _queue_limit():
        await _reply_text_with_retry(
            turn.msg,
            _with_task_banner(
                job,
                f"等待佇列已滿（上限 {_queue_limit()} 則），請稍後再傳。",
            ),
            job=job,
            registry=registry,
            role="error",
        )
        return False
    queue.append(turn)
    extra = f"（task {turn.task_id}）" if turn.task_id else ""
    try:
        await _reply_text_with_retry(
            turn.msg,
            _with_task_banner(
                job,
                f"已加入等待佇列{extra}，目前順位 {len(queue)}；前一項完成後會自動處理並回覆。",
            ),
            job=job,
            registry=registry,
            role="ack",
        )
    except Exception:
        _discard_queued_turn(_PENDING_TURNS, turn.key, turn)
        if turn.task_id:
            TaskRegistry(turn.store_path).update_status(turn.task_id, "failed")
        _schedule_queue_drain(turn.key)
        raise
    _schedule_queue_drain(turn.key)
    return True


async def _on_message(update, context) -> None:  # type: ignore[no-untyped-def]
    if update.effective_user is None or update.effective_chat is None:
        return
    if not _check_auth(update):
        log.warning("拒絕未授權使用者: %s", update.effective_user.id)
        return

    msg = update.message
    if msg is None:
        return

    text = (msg.text or "").strip()
    has_attachment = extract_incoming_attachment(msg) is not None

    if not text and not has_attachment:
        await update.effective_chat.send_message(_supported_types_hint())
        return

    attachment_config = _attachment_config()
    prompt: str | None = None

    if has_attachment:
        downloaded, dl_err = await _download_incoming_attachment(msg, attachment_config)
        if dl_err:
            await _reply_text_with_retry(msg, dl_err)
            return
        if downloaded is None:
            await _reply_text_with_retry(msg, "無法處理附件。")
            return
        prompt = build_attachment_prompt(downloaded)
        try:
            cleanup_upload_dirs(attachment_config)
        except Exception:
            log.exception("upload retention cleanup 失敗")
    elif text:
        prompt = text
    else:
        await update.effective_chat.send_message(_supported_types_hint())
        return

    chat = update.effective_chat
    thread_id = msg.message_thread_id
    key = telegram_thread_key(chat_type=str(chat.type), ids=TelegramIds(chat.id, thread_id))

    decision = route_text(prompt, load_task_router_config().config)
    store_path = Path(os.environ.get("TGR_SESSION_DB", str(Path("data") / "sessions.sqlite3")))
    store = SessionStore(store_path)
    registry = TaskRegistry(store_path)
    backend = _backend_for_thread(store, key)

    reply_id = _reply_to_message_id(msg)
    if reply_id is not None:
        bound = registry.get_task_for_message(key, reply_id)
        if bound is None:
            # Parent replies are explicit session routing, even when other
            # background tasks exist or a different task has current focus.
            parent_backend = registry.get_parent_message_backend(key, reply_id)
            if parent_backend is not None:
                if (
                    parent_backend == "codex"
                    and key in _ACTIVE_RELAY_THREADS
                    and os.environ.get("TGR_CODEX_TRANSPORT", "cli").lower() == "app-server"
                    and os.environ.get("TGR_CODEX_APP_STEER", "1").lower()
                    not in ("0", "false", "no", "off")
                ):
                    # Explicit Reply to the main Codex session is an instruction
                    # to that running turn, not a new task. If steer races with
                    # completion or is unsupported, use the existing FIFO queue.
                    steered = await asyncio.to_thread(
                        steer_codex_turn, key, prompt,
                        os.environ.get("TGR_CODEX_BIN", "codex"),
                    )
                    if steered:
                        registry.bind_parent_message(
                            parent_key=key, message_id=msg.message_id,
                            backend="codex",
                        )
                        await _reply_text_with_retry(
                            msg, "已追加到目前執行中的 Codex 工作。",
                            registry=registry, parent_key=key, parent_backend="codex",
                        )
                        return
                turn = QueuedTurn(
                    msg=msg, key=key, backend=parent_backend,
                    prompt=prompt, store_path=store_path,
                    attachment_config=attachment_config, route=Route.DIRECT,
                )
                if key in _ACTIVE_RELAY_THREADS or _PENDING_TURNS.get(key):
                    await _enqueue_turn(turn)
                else:
                    await _run_direct_turn(turn)
                return
            tasks = registry.list_tasks(key)
            if tasks:
                await _reply_text_with_retry(msg, _unbound_reply_instructions(tasks))
                return
        elif bound.parent_key != key:
            await _reply_text_with_retry(
                msg,
                f"任務 {bound.task_id} [{bound.title}] 不屬於這個對話／主題，無法在此繼續。",
            )
            return
        else:
            registry.focus_task(key, bound.task_id)
            if _is_progress_query(prompt):
                job = _RUNNING_TASKS.get(bound.task_id)
                await _reply_task_status(msg, bound, registry, job)
                return
            await _resume_existing_task(
                bound,
                msg=msg,
                prompt=prompt,
                store_path=store_path,
                attachment_config=attachment_config,
                registry=registry,
            )
            return

    active_job = _ACTIVE_JOBS.get(key)
    tasks = registry.list_tasks(key)
    focused = registry.focused_task(key)

    # References to the ongoing task take precedence over new-task classification.
    if _is_progress_query(prompt):
        if focused is not None:
            await _reply_task_status(msg, focused, registry, _RUNNING_TASKS.get(focused.task_id))
            return
        running = [t for t in tasks if t.status in ("queued", "starting", "running")]
        if len(running) == 1:
            await _reply_task_status(msg, running[0], registry, _RUNNING_TASKS.get(running[0].task_id))
            return
        if len(tasks) == 1:
            await _reply_task_status(msg, tasks[0], registry, _RUNNING_TASKS.get(tasks[0].task_id))
            return
        if tasks:
            if _task_context_enabled():
                turn = QueuedTurn(
                    msg=msg, key=key, backend=backend, prompt=prompt,
                    store_path=store_path, attachment_config=attachment_config,
                    route=Route.DIRECT, kind="resolve_status",
                )
                if key in _ACTIVE_RELAY_THREADS or _PENDING_TURNS.get(key):
                    await _enqueue_turn(turn)
                else:
                    await _run_direct_turn(turn)
            else:
                await _reply_text_with_retry(msg, _task_choice_text(tasks))
            return
        if active_job is not None:
            await _reply_text_with_retry(msg, _job_status_text(active_job))
            return

    bare_continuation = _is_bare_continuation(prompt)
    contextual_followup = _is_contextual_followup(prompt)
    if bare_continuation or contextual_followup:
        # An explicit focus + a bare "continue", or a single possible task,
        # can bypass semantic resolution. Other focus is only a hint.
        target = (focused if bare_continuation else None) or (
            tasks[0] if len(tasks) == 1 else None
        )
        if target is not None:
            await _resume_existing_task(
                target, msg=msg, prompt=prompt, store_path=store_path,
                attachment_config=attachment_config, registry=registry,
            )
            return

    if tasks and (
        bare_continuation or contextual_followup
        or might_refer_to_task(prompt, tasks)
    ):
        if _task_context_enabled():
            turn = QueuedTurn(
                msg=msg, key=key, backend=backend, prompt=prompt,
                store_path=store_path, attachment_config=attachment_config,
                route=Route.DIRECT, kind="resolve_task",
            )
            if key in _ACTIVE_RELAY_THREADS or _PENDING_TURNS.get(key):
                await _enqueue_turn(turn)
            else:
                await _run_direct_turn(turn)
            return
        if bare_continuation or contextual_followup:
            await _reply_text_with_retry(msg, _task_choice_text(tasks))
            return

    if decision.route == Route.DELEGATE:
        turn = QueuedTurn(
            msg=msg, key=key, backend=backend, prompt=prompt,
            store_path=store_path, attachment_config=attachment_config,
            route=decision.route,
        )
        busy = active_job is not None or key in _ACTIVE_RELAY_THREADS or bool(_PENDING_TURNS.get(key))
        _register_new_task(turn, store, registry, "queued" if busy else "starting")
        if not busy:
            await _start_background_job(turn)
            return
        queued_ok = await _enqueue_turn(turn)
        if not queued_ok:
            registry.update_status(turn.task_id, "failed")
        return

    turn = QueuedTurn(
        msg=msg,
        key=key,
        backend=backend,
        prompt=prompt,
        store_path=store_path,
        attachment_config=attachment_config,
        route=decision.route,
    )

    if key in _ACTIVE_RELAY_THREADS or _PENDING_TURNS.get(key):
        await _enqueue_turn(turn)
        return
    await _run_direct_turn(turn)


def _bot_singleton_lock_path(token: str) -> Path:
    """Return a per-bot lock path so multiple relay bots can share one data dir.

    The singleton guard is meant to prevent two polling processes from using the
    same Telegram bot token (409 Conflict). It must not block separate bots such
    as Claude and Cursor when their session DBs live in the same directory.
    """
    explicit = os.environ.get("TGR_BOT_LOCK_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser()

    base_dir = Path(os.environ.get("TGR_SESSION_DB", "data/sessions.sqlite3")).parent
    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    return base_dir / f"bot-{fingerprint}.lock"


def _acquire_bot_singleton(token: str) -> None:
    """避免多個 polling 實例搶同一 token（Telegram 409 Conflict）。"""
    global _BOT_LOCK_FILE

    lock_path = _bot_singleton_lock_path(token)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("w")

    import sys

    try:
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as e:
                raise RuntimeError(
                    "已有另一個 tg_cli_relay bot 在執行。"
                    "請先結束舊程序再啟動，否則 Telegram 會回 409 Conflict。"
                ) from e
        else:
            import fcntl

            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise RuntimeError("已有另一個 tg_cli_relay bot 在執行。") from e
    except Exception:
        lock_file.close()
        raise

    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    _BOT_LOCK_FILE = lock_file


def run_bot() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("請設定 TELEGRAM_BOT_TOKEN")

    _acquire_bot_singleton(token)

    log_level = os.environ.get("TGR_LOG_LEVEL", "INFO")
    log_dir = Path(os.environ.get("TGR_SESSION_DB", "data/sessions.sqlite3")).parent
    log_file = log_dir / "bot.log"
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

    from telegram.ext import CommandHandler, MessageHandler, filters

    app = build_resilient_application(token)
    app.add_handler(CommandHandler("reset", _cmd_reset))
    app.add_handler(CommandHandler("new", _cmd_reset))
    app.add_handler(CommandHandler("status", _cmd_status))
    app.add_handler(CommandHandler("provider", _cmd_provider))
    app.add_handler(CommandHandler("model", _cmd_model))
    app.add_handler(CommandHandler("router", _cmd_router))
    app.add_handler(CommandHandler("task", _cmd_task))
    app.add_handler(CommandHandler("jobs", _cmd_jobs))
    app.add_handler(CommandHandler("job", _cmd_job))
    app.add_handler(CommandHandler("help", _cmd_help))
    content_filter = (
        filters.TEXT | filters.Document.ALL | filters.PHOTO | filters.AUDIO | filters.VOICE | filters.VIDEO
    ) & ~filters.COMMAND
    # block=False isolates each relay turn from Telegram's update loop.  A slow
    # provider can no longer prevent commands or other chats from being handled.
    app.add_handler(MessageHandler(content_filter, _on_message, block=False))
    store_path = Path(os.environ.get("TGR_SESSION_DB", str(Path("data") / "sessions.sqlite3")))
    interrupted = TaskRegistry(store_path).interrupt_inflight()
    # Existing installations may still have jobs created by the earlier
    # JobStore dispatcher. Preserve their history, but never auto-rerun them.
    try:
        from tg_cli_relay.job_store import JobStore
        JobStore(store_path).mark_interrupted_jobs()
    except ImportError:
        pass
    if interrupted:
        log.warning("啟動時將 %s 筆 queued/running 任務標為 interrupted，不會自動重跑", interrupted)
    log.info("啟動 Telegram bot（default_backend=%s）", _default_backend())
    app.run_polling(allowed_updates=["message"], drop_pending_updates=True, bootstrap_retries=-1)
