from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
from typing import Iterable

import httpx

log = logging.getLogger(__name__)

TELEGRAM_API_HOST = "api.telegram.org"
_DOH_TIMEOUT_SECONDS = 4.0
_DOH_PROVIDERS: tuple[dict[str, object], ...] = (
    {
        "url": "https://dns.google/resolve",
        "params": {"name": TELEGRAM_API_HOST, "type": "A"},
        "headers": {},
    },
    {
        "url": "https://cloudflare-dns.com/dns-query",
        "params": {"name": TELEGRAM_API_HOST, "type": "A"},
        "headers": {"Accept": "application/dns-json"},
    },
)
_SEED_FALLBACK_IPS: tuple[str, ...] = ("149.154.167.220",)


def parse_fallback_ip_env(raw: str | None) -> list[str]:
    if not raw:
        return []
    return _normalize_fallback_ips(part.strip() for part in raw.split(","))


def resolve_proxy_url() -> str | None:
    value = os.environ.get("TGR_TELEGRAM_PROXY", "").strip()
    return value or None


def _normalize_fallback_ips(values: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        raw = str(value).strip()
        if not raw:
            continue
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            log.warning("忽略無效的 Telegram fallback IP: %r", raw)
            continue
        if addr.version != 4:
            log.warning("忽略非 IPv4 的 Telegram fallback IP: %s", raw)
            continue
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_unspecified:
            log.warning("忽略內網/保留位址 fallback IP: %s", raw)
            continue
        text = str(addr)
        if text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def rewrite_request_for_ip(request: httpx.Request, ip: str) -> httpx.Request:
    original_host = request.url.host or TELEGRAM_API_HOST
    url = request.url.copy_with(host=ip)
    headers = request.headers.copy()
    headers["host"] = original_host
    extensions = dict(request.extensions)
    extensions["sni_hostname"] = original_host
    return httpx.Request(
        method=request.method,
        url=url,
        headers=headers,
        stream=request.stream,
        extensions=extensions,
    )


def is_retryable_connect_error(exc: Exception) -> bool:
    return isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError))


def _resolve_system_dns() -> set[str]:
    try:
        entries = socket.getaddrinfo(TELEGRAM_API_HOST, 443, socket.AF_INET)
    except Exception:
        return set()
    return {entry[4][0] for entry in entries}


async def _query_doh_provider(client: httpx.AsyncClient, provider: dict[str, object]) -> list[str]:
    try:
        resp = await client.get(
            str(provider["url"]),
            params=dict(provider["params"]),  # type: ignore[arg-type]
            headers=dict(provider["headers"]),  # type: ignore[arg-type]
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.debug("DoH 查詢失敗 %s: %s", provider.get("url"), exc)
        return []

    ips: list[str] = []
    for answer in data.get("Answer", []):
        if answer.get("type") != 1:
            continue
        raw = str(answer.get("data", "")).strip()
        try:
            ipaddress.ip_address(raw)
        except ValueError:
            continue
        ips.append(raw)
    return ips


async def discover_fallback_ips_async() -> list[str]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(_DOH_TIMEOUT_SECONDS)) as client:
        tasks = [_query_doh_provider(client, provider) for provider in _DOH_PROVIDERS]
        system_dns_task = asyncio.to_thread(_resolve_system_dns)
        results = await asyncio.gather(system_dns_task, *tasks, return_exceptions=True)

    discovered: list[str] = []
    for item in results[1:]:
        if isinstance(item, list):
            discovered.extend(item)
    normalized = _normalize_fallback_ips(discovered)
    if normalized:
        log.debug("DoH fallback IP: %s", ",".join(normalized))
        return normalized

    system_ips = results[0] if isinstance(results[0], set) else set()
    log.info(
        "DoH 未取得可用 IP（system DNS=%s），改用 seed IP %s",
        ",".join(system_ips) or "unknown",
        ",".join(_SEED_FALLBACK_IPS),
    )
    return list(_SEED_FALLBACK_IPS)


def discover_fallback_ips() -> list[str]:
    try:
        return asyncio.run(discover_fallback_ips_async())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(discover_fallback_ips_async())
        finally:
            loop.close()


def load_fallback_ips() -> list[str]:
    from_env = parse_fallback_ip_env(os.environ.get("TGR_TELEGRAM_FALLBACK_IPS"))
    if from_env:
        return from_env
    return discover_fallback_ips()


class TelegramFallbackTransport(httpx.AsyncBaseTransport):
    def __init__(self, fallback_ips: Iterable[str], **transport_kwargs) -> None:
        self._fallback_ips = _normalize_fallback_ips(fallback_ips)
        proxy = resolve_proxy_url()
        if proxy and "proxy" not in transport_kwargs:
            transport_kwargs["proxy"] = proxy
        self._primary = httpx.AsyncHTTPTransport(**transport_kwargs)
        self._fallback_transports = {
            ip: httpx.AsyncHTTPTransport(**transport_kwargs) for ip in self._fallback_ips
        }
        self._sticky_ip: str | None = None
        self._sticky_lock = asyncio.Lock()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != TELEGRAM_API_HOST or not self._fallback_ips:
            return await self._primary.handle_async_request(request)

        sticky_ip = self._sticky_ip
        order: list[str | None] = [sticky_ip] if sticky_ip else [None]
        if sticky_ip:
            order.append(None)
        for ip in self._fallback_ips:
            if ip != sticky_ip:
                order.append(ip)

        last_error: Exception | None = None
        for ip in order:
            candidate = request if ip is None else rewrite_request_for_ip(request, ip)
            transport = self._primary if ip is None else self._fallback_transports[ip]
            try:
                response = await transport.handle_async_request(candidate)
                if ip and self._sticky_ip != ip:
                    async with self._sticky_lock:
                        if self._sticky_ip != ip:
                            self._sticky_ip = ip
                            log.warning("Telegram 連線切換至 fallback IP: %s", ip)
                return response
            except Exception as exc:
                last_error = exc
                if not is_retryable_connect_error(exc):
                    raise
                if ip and ip == self._sticky_ip:
                    async with self._sticky_lock:
                        if self._sticky_ip == ip:
                            self._sticky_ip = None
                if ip is None:
                    log.warning("Telegram 主要路徑連線失敗，改試 fallback IP: %s", exc)
                else:
                    log.warning("Telegram fallback IP %s 連線失敗: %s", ip, exc)

        if last_error is None:
            raise RuntimeError("Telegram fallback 嘗試失敗且未取得錯誤資訊")
        raise last_error

    async def aclose(self) -> None:
        await self._primary.aclose()
        for transport in self._fallback_transports.values():
            await transport.aclose()
