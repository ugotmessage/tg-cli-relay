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


def _backend() -> Backend:
    b = os.environ.get("TGR_BACKEND", "cursor").strip().lower()
    if b in ("cursor", "codex", "claude"):
        return b  # type: ignore[return-value]
    raise RuntimeError("TGR_BACKEND 必須是 cursor、codex 或 claude")


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
    backend = _backend()
    _get_store().delete(key, backend)
    await update.message.reply_text(f"對話已重置，下一則訊息將開啟新的 {backend} session。")


async def _cmd_status(update, context) -> None:  # type: ignore[no-untyped-def]
    if not _check_auth(update):
        return
    key = _get_thread_key(update)
    backend = _backend()
    store = _get_store()
    sid = store.get(key, backend)
    ws = os.environ.get("TGR_DEFAULT_WORKSPACE", "(未設定)")
    lines = [
        f"後端: {backend}",
        f"工作目錄: {ws}",
        f"Session: {(sid[:8] + '...') if sid else '（尚未建立）'}",
    ]
    if backend == "claude":
        model = store.get_pref(key, "model") or "(預設)"
        lines.append(f"模型: {model}")
    await update.message.reply_text("\n".join(lines))


async def _cmd_model(update, context) -> None:  # type: ignore[no-untyped-def]
    if not _check_auth(update):
        return

    backend = _backend()
    key = _get_thread_key(update)
    store = _get_store()
    args: list[str] = context.args or []

    if backend == "claude":
        from tg_cli_relay.providers.claude_cli import CLAUDE_MODELS

        if not args:
            current = store.get_pref(key, "model") or "(預設)"
            model_list = "\n".join(f"  {m}" for m in CLAUDE_MODELS)
            await update.message.reply_text(
                f"目前模型: {current}\n\n可用模型:\n{model_list}\n\n用法: /model <模型名稱>"
            )
            return
        chosen = args[0]
        if chosen not in CLAUDE_MODELS:
            await update.message.reply_text(
                f"不支援的模型: {chosen}\n可用: {', '.join(CLAUDE_MODELS)}"
            )
            return
        store.set_pref(key, "model", chosen)
        await update.message.reply_text(f"模型已切換至 {chosen}（下一輪起生效）。")

    elif backend == "cursor":
        from tg_cli_relay.providers.cursor_agent import CURSOR_MODELS

        if not args:
            current = store.get_pref(key, "model") or "(預設 auto)"
            model_list = "\n".join(f"  {m}" for m in CURSOR_MODELS)
            await update.message.reply_text(
                f"目前模型: {current}\n\n常用模型（精選）:\n{model_list}\n\n"
                f"完整清單執行 `agent --list-models`\n用法: /model <model-id>"
            )
            return
        chosen = args[0]
        store.set_pref(key, "model", chosen)
        await update.message.reply_text(f"模型已切換至 {chosen}（下一輪起生效）。")

    elif backend == "codex":
        from tg_cli_relay.providers.codex_cli import CODEX_MODELS

        if not args:
            current = store.get_pref(key, "model") or "(預設)"
            model_list = "\n".join(f"  {m}" for m in CODEX_MODELS)
            await update.message.reply_text(
                f"目前模型: {current}\n\n可用模型:\n{model_list}\n\n"
                f"用法: /model <model-name>"
            )
            return
        chosen = args[0]
        store.set_pref(key, "model", chosen)
        await update.message.reply_text(f"模型已切換至 {chosen}（下一輪起生效）。")

    else:
        await update.message.reply_text(f"此後端（{backend}）不支援 /model 指令。")


async def _cmd_help(update, context) -> None:  # type: ignore[no-untyped-def]
    if not _check_auth(update):
        return
    backend = _backend()
    lines = [
        "/reset — 清除對話，開啟新 session",
        "/status — 查看目前後端與 session 狀態",
        "/help — 顯示此說明",
    ]
    if backend in ("claude", "cursor", "codex"):
        lines.insert(2, "/model [名稱] — 查看或切換模型")
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
    try:
        # relay_turn 內部會跑 subprocess，改放到 thread 避免阻塞 event loop，
        # 才能持續送出 Telegram typing 狀態。
        res = await asyncio.to_thread(
            relay_turn,
            thread_key=key,
            backend=_backend(),
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
    body = out if res.returncode == 0 else f"{out}\n\n--- stderr ---\n{err}".strip()

    backend = _backend()
    sid = store.get(key, backend)
    model = store.get_pref(key, "model")
    footer_parts = []
    if model:
        footer_parts.append(model)
    if sid:
        footer_parts.append(f"session:{sid}")
    footer = "\n\n— " + " · ".join(footer_parts) if footer_parts else ""

    chunks = _chunk_reply(body)
    for i, part in enumerate(chunks):
        await msg.reply_text(part + (footer if i == len(chunks) - 1 else ""))


def run_bot() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("請設定 TELEGRAM_BOT_TOKEN")

    logging.basicConfig(level=os.environ.get("TGR_LOG_LEVEL", "INFO"))

    from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, filters

    app: Application = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("reset", _cmd_reset))
    app.add_handler(CommandHandler("new", _cmd_reset))
    app.add_handler(CommandHandler("status", _cmd_status))
    app.add_handler(CommandHandler("model", _cmd_model))
    app.add_handler(CommandHandler("help", _cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
    log.info("啟動 Telegram bot（backend=%s）", _backend())
    app.run_polling(allowed_updates=["message"])
