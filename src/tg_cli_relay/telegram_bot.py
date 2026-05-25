from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from tg_cli_relay.relay import relay_turn
from tg_cli_relay.session_store import Backend, SessionStore
from tg_cli_relay.thread_key import TelegramIds, telegram_thread_key

log = logging.getLogger(__name__)

TG_CHUNK = 3800
_BOT_LOCK_FILE = None
SUPPORTED_BACKENDS: tuple[Backend, ...] = ("cursor", "codex", "claude", "opencode")


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


def _format_model_catalog(store: SessionStore, thread_key: str, active_backend: Backend) -> str:
    lines = [f"目前 provider: {active_backend}", ""]
    for backend in _enabled_backends():
        marker = "*" if backend == active_backend else "-"
        current = store.get_pref(thread_key, _model_pref_key(backend)) or "(預設)"
        lines.append(f"{marker} {backend} 目前模型: {current}")
        models = _models_for_backend(backend)
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
    store = _get_store()
    backend = _backend_for_thread(store, key)
    store.delete(key, backend)
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
        f"模型: {store.get_pref(key, _model_pref_key(backend)) or '(預設)'}",
        f"可用 providers: {', '.join(_enabled_backends())}",
    ]
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
        await update.message.reply_text(_format_model_catalog(store, key, backend))
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
            current = store.get_pref(key, _model_pref_key(backend)) or "(預設)"
            models = "\n".join(f"  {m}" for m in _models_for_backend(backend))
            await update.message.reply_text(
                f"Provider 已切換至 {backend}\n目前模型: {current}\n\n可用模型:\n{models}"
            )
            return
        chosen = args[1]
    else:
        chosen = args[0]

    models = _models_for_backend(backend)
    if models and chosen not in models:
        await update.message.reply_text(f"不支援的模型: {chosen}\n可用: {', '.join(models)}")
        return

    store.set_pref(key, _model_pref_key(backend), chosen)
    await update.message.reply_text(f"{backend} 模型已切換至 {chosen}（下一輪起生效）。")


async def _cmd_help(update, context) -> None:  # type: ignore[no-untyped-def]
    if not _check_auth(update):
        return
    lines = [
        "/reset — 清除對話，開啟新 session",
        "/status — 查看目前後端與 session 狀態",
        "/provider [名稱] — 查看或切換 CLI provider",
        "/model [provider] [名稱] — 查看或切換模型",
        "/help — 顯示此說明",
    ]
    await update.message.reply_text("\n".join(lines))


def _chunk_reply(text: str) -> list[str]:
    t = text or ""
    if len(t) <= TG_CHUNK:
        return [t] if t else ["(無輸出)"]
    return [t[i : i + TG_CHUNK] for i in range(0, len(t), TG_CHUNK)]


async def _on_message(update, context) -> None:  # type: ignore[no-untyped-def]
    if update.effective_user is None or update.effective_chat is None:
        return
    allow = _allowed_ids()
    if allow is not None and update.effective_user.id not in allow:
        log.warning("拒絕未授權使用者: %s", update.effective_user.id)
        return

    msg = update.message
    if msg is None or not (msg.text and msg.text.strip()):
        await update.effective_chat.send_message("請傳送純文字訊息。")
        return

    # 先顯示 typing，讓使用者知道訊息已收到且正在處理。
    from telegram.constants import ChatAction

    async def _typing_loop() -> None:
        while True:
            await msg.chat.send_action(action=ChatAction.TYPING)
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(_typing_loop())

    chat = update.effective_chat
    thread_id = msg.message_thread_id
    key = telegram_thread_key(chat_type=str(chat.type), ids=TelegramIds(chat.id, thread_id))

    store_path = Path(os.environ.get("TGR_SESSION_DB", str(Path("data") / "sessions.sqlite3")))
    store = SessionStore(store_path)
    backend = _backend_for_thread(store, key)
    try:
        # relay_turn 內部會跑 subprocess，改放到 thread 避免阻塞 event loop，
        # 才能持續送出 Telegram typing 狀態。
        res = await asyncio.to_thread(
            relay_turn,
            thread_key=key,
            backend=backend,
            prompt=msg.text.strip(),
            store=store,
        )
    except Exception:
        log.exception("relay_turn 失敗 thread=%s", key)
        await msg.reply_text("執行失敗，請查看伺服器日誌。")
        return
    finally:
        typing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await typing_task

    out = res.stdout or ""
    err = res.stderr or ""
    if res.returncode == 0:
        body = out
    elif err.strip():
        body = f"{out}\n\n--- stderr ---\n{err}".strip()
    else:
        body = out

    sid = store.get(key, backend)
    model = store.get_pref(key, _model_pref_key(backend))
    footer_parts = []
    footer_parts.append(backend)
    if model:
        footer_parts.append(model)
    if sid:
        footer_parts.append(f"session:{sid}")
    footer = "\n\n— " + " · ".join(footer_parts) if footer_parts else ""

    chunks = _chunk_reply(body)
    for i, part in enumerate(chunks):
        await msg.reply_text(part + (footer if i == len(chunks) - 1 else ""))


def _acquire_bot_singleton() -> None:
    """避免多個 polling 實例搶同一 token（Telegram 409 Conflict）。"""
    global _BOT_LOCK_FILE

    lock_path = Path(os.environ.get("TGR_SESSION_DB", "data/sessions.sqlite3")).parent / "bot.lock"
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

    _acquire_bot_singleton()

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

    from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, filters

    app: Application = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("reset", _cmd_reset))
    app.add_handler(CommandHandler("new", _cmd_reset))
    app.add_handler(CommandHandler("status", _cmd_status))
    app.add_handler(CommandHandler("provider", _cmd_provider))
    app.add_handler(CommandHandler("model", _cmd_model))
    app.add_handler(CommandHandler("help", _cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
    log.info("啟動 Telegram bot（default_backend=%s）", _default_backend())
    app.run_polling(allowed_updates=["message"], drop_pending_updates=True)
