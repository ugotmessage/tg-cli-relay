from tg_cli_relay.telegram_bot import _bot_singleton_lock_path


def test_bot_singleton_lock_is_per_token_with_shared_session_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TGR_SESSION_DB", str(tmp_path / "sessions.sqlite3"))
    monkeypatch.delenv("TGR_BOT_LOCK_PATH", raising=False)

    first = _bot_singleton_lock_path("123:CLAUDE")
    second = _bot_singleton_lock_path("456:CURSOR")

    assert first.parent == tmp_path
    assert second.parent == tmp_path
    assert first.name.startswith("bot-")
    assert second.name.startswith("bot-")
    assert first != second
    assert "CLAUDE" not in first.name
    assert "CURSOR" not in second.name


def test_bot_singleton_lock_explicit_override(monkeypatch, tmp_path):
    explicit = tmp_path / "custom.lock"
    monkeypatch.setenv("TGR_BOT_LOCK_PATH", str(explicit))

    assert _bot_singleton_lock_path("123:ANY") == explicit
