from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TelegramIds:
    chat_id: int
    message_thread_id: int | None = None


def telegram_thread_key(*, chat_type: str, ids: TelegramIds) -> str:
    """將 Telegram 對話映射成穩定的 thread key。"""
    ct = chat_type.lower().strip()
    if ct in ("private", "channel"):
        return f"{ct}:{ids.chat_id}"
    if ct in ("group", "supergroup"):
        if ids.message_thread_id is not None:
            return f"topic:{ids.chat_id}:{ids.message_thread_id}"
        return f"group:{ids.chat_id}"
    return f"other:{ct}:{ids.chat_id}"
