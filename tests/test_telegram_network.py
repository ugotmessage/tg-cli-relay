import httpx

from tg_cli_relay import telegram_network


def test_parse_fallback_ip_env_filters_invalid_values():
    raw = "149.154.167.220, ,149.154.167.220,127.0.0.1,invalid,2001:db8::1,149.154.166.110"
    parsed = telegram_network.parse_fallback_ip_env(raw)
    assert parsed == ["149.154.167.220", "149.154.166.110"]


def test_rewrite_request_for_ip_preserves_host_and_sni():
    req = httpx.Request("GET", "https://api.telegram.org/bot123/getMe")
    rewritten = telegram_network.rewrite_request_for_ip(req, "149.154.167.220")
    assert rewritten.url.host == "149.154.167.220"
    assert rewritten.headers["host"] == "api.telegram.org"
    assert rewritten.extensions["sni_hostname"] == "api.telegram.org"


def test_load_fallback_ips_prefers_env(monkeypatch):
    monkeypatch.setenv("TGR_TELEGRAM_FALLBACK_IPS", "149.154.166.110")
    monkeypatch.setattr(telegram_network, "discover_fallback_ips", lambda: ["149.154.167.220"])
    assert telegram_network.load_fallback_ips() == ["149.154.166.110"]
