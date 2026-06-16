from tg_cli_relay import telegram_app


def test_build_resilient_application_wires_requests(monkeypatch):
    captured: dict[str, object] = {}

    class FakeRequest:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeBuilder:
        def __init__(self):
            captured["builder"] = self

        def token(self, token):
            captured["token"] = token
            return self

        def request(self, request):
            captured["request"] = request
            return self

        def get_updates_request(self, request):
            captured["updates_request"] = request
            return self

        def build(self):
            captured["built"] = True
            return "APP"

    class FakeTransport:
        def __init__(self, fallback_ips):
            self.fallback_ips = list(fallback_ips)

    monkeypatch.setenv("TGR_TELEGRAM_CONNECT_TIMEOUT", "45")
    monkeypatch.setenv("TGR_TELEGRAM_READ_TIMEOUT", "75")
    monkeypatch.setattr(telegram_app, "load_fallback_ips", lambda: ["149.154.167.220"])
    monkeypatch.setattr(telegram_app, "HTTPXRequest", FakeRequest)
    monkeypatch.setattr(telegram_app, "TelegramFallbackTransport", FakeTransport)
    monkeypatch.setattr(telegram_app, "ApplicationBuilder", FakeBuilder)

    app = telegram_app.build_resilient_application("123:ABC")

    assert app == "APP"
    assert captured["token"] == "123:ABC"
    request = captured["request"]
    updates_request = captured["updates_request"]
    assert isinstance(request, FakeRequest)
    assert isinstance(updates_request, FakeRequest)
    assert request.kwargs["connect_timeout"] == 45.0
    assert request.kwargs["read_timeout"] == 75.0
    assert updates_request.kwargs["connect_timeout"] == 45.0
    assert updates_request.kwargs["read_timeout"] == 75.0
    transport = request.kwargs["httpx_kwargs"]["transport"]
    assert isinstance(transport, FakeTransport)
    assert transport.fallback_ips == ["149.154.167.220"]
