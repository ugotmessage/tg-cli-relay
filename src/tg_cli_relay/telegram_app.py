from __future__ import annotations

import os

from telegram.ext import Application, ApplicationBuilder
from telegram.request import HTTPXRequest

from tg_cli_relay.telegram_network import TelegramFallbackTransport, load_fallback_ips


def _read_timeout_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def build_resilient_application(token: str) -> Application:
    connect_timeout = _read_timeout_env("TGR_TELEGRAM_CONNECT_TIMEOUT", 30.0)
    read_timeout = _read_timeout_env("TGR_TELEGRAM_READ_TIMEOUT", 60.0)
    fallback_ips = load_fallback_ips()

    request = HTTPXRequest(
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        httpx_kwargs={"transport": TelegramFallbackTransport(fallback_ips)},
    )
    updates_request = HTTPXRequest(
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        httpx_kwargs={"transport": TelegramFallbackTransport(fallback_ips)},
    )

    return (
        ApplicationBuilder()
        .token(token)
        .request(request)
        .get_updates_request(updates_request)
        .build()
    )
