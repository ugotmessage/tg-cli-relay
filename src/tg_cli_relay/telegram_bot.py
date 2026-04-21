from __future__ import annotations

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
    if b in ("cursor", "codex"):
        return b  # type: ignore[return-value]
    raise RuntimeError("TGR_BACKEND 必須是 cursor 或 codex")


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

    chat = update.effective_chat
    thread_id = msg.message_thread_id
    key = telegram_thread_key(chat_type=str(chat.type), ids=TelegramIds(chat.id, thread_id))

    store_path = Path(os.environ.get("TGR_SESSION_DB", str(Path("data") / "sessions.sqlite3")))
    store = SessionStore(store_path)
    try:
        res = relay_turn(
            thread_key=key,
            backend=_backend(),
            prompt=msg.text.strip(),
            store=store,
        )
    except Exception:
        log.exception("relay_turn 失敗 thread=%s", key)
        await msg.reply_text("執行失敗，請查看伺服器日誌。")
        return

    out = res.stdout or ""
    err = res.stderr or ""
    body = out if res.returncode == 0 else f"{out}\n\n--- stderr ---\n{err}".strip()
    for part in _chunk_reply(body):
        await msg.reply_text(part)


def run_bot() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("請設定 TELEGRAM_BOT_TOKEN")

    logging.basicConfig(level=os.environ.get("TGR_LOG_LEVEL", "INFO"))

    from telegram.ext import Application, ApplicationBuilder, MessageHandler, filters

    app: Application = ApplicationBuilder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
    log.info("啟動 Telegram bot（backend=%s）", _backend())
    app.run_polling(allowed_updates=["message"])
