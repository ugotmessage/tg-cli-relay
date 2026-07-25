"""Tests for Telegram attachment download, TGR_FILE outbound, and cleanup."""

from __future__ import annotations

import asyncio
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tg_cli_relay import attachments
from tg_cli_relay.attachments import (
    AttachmentConfig,
    DEFAULT_ALLOWED_DOWNLOAD_MIME_TYPES,
    IncomingAttachment,
    build_attachment_prompt,
    cleanup_upload_dirs,
    download_attachment_to_path,
    extract_incoming_attachment,
    is_attachment_mime_confirmed,
    is_incoming_attachment_allowed,
    parse_tgr_file_markers,
    prepare_download_path,
    resolve_outbound_files,
    sanitize_filename,
    validate_upload_path,
)
from tg_cli_relay.providers.base import RunResult


def _cfg(tmp_path: Path, **overrides) -> AttachmentConfig:
    upload = tmp_path / "uploads"
    upload.mkdir(parents=True, exist_ok=True)
    base = AttachmentConfig(
        upload_dir=upload,
        max_download_bytes=1024,
        max_upload_bytes=1024,
        allowed_download_mime_types=frozenset({"text/plain", "application/pdf", "image/jpeg"}),
        allowed_upload_roots=(upload.resolve(),),
        max_files_per_message=2,
        upload_retention_hours=1,
        bot_fingerprint="testbot",
    )
    for k, v in overrides.items():
        object.__setattr__(base, k, v)
    return base


def _doc_msg(**kwargs):
    defaults = dict(
        document=SimpleNamespace(
            file_id="fid",
            file_unique_id="uniq1",
            file_name="report.pdf",
            mime_type="application/pdf",
            file_size=100,
        ),
        photo=None,
        audio=None,
        voice=None,
        video=None,
        caption="請摘要",
        text=None,
        chat_id=42,
        message_id=99,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults, get_bot=lambda: MagicMock())


def test_document_with_caption_download_and_prompt(tmp_path):
    cfg = _cfg(tmp_path, max_download_bytes=4096)
    msg = _doc_msg()
    incoming = extract_incoming_attachment(msg)
    assert incoming is not None
    dest = prepare_download_path(cfg, msg.chat_id, msg.message_id, incoming, set())

    async def _run():
        bot = AsyncMock()

        async def _get_file(file_id):
            assert file_id == "fid"
            f = AsyncMock()

            async def _dl(**kwargs):
                Path(kwargs["custom_path"]).write_bytes(b"%PDF-1.4 tiny")

            f.download_to_drive = _dl
            return f

        bot.get_file = _get_file
        size = await download_attachment_to_path(bot, incoming, dest, max_bytes=4096)
        assert size == len(b"%PDF-1.4 tiny")
        assert dest.exists()
        assert oct(dest.stat().st_mode & 0o777) == oct(stat.S_IRUSR | stat.S_IWUSR)

        downloaded = attachments.DownloadedAttachment(
            local_path=dest,
            original_name=incoming.original_name or dest.name,
            mime_type=incoming.mime_type,
            mime_confirmed=True,
            size_bytes=size,
            caption=msg.caption,
        )
        prompt = build_attachment_prompt(downloaded)
        assert str(dest) in prompt
        assert "report.pdf" in prompt
        assert "application/pdf" in prompt
        assert "請摘要" in prompt

    asyncio.run(_run())


def test_photo_selects_highest_resolution():
    small = SimpleNamespace(file_id="s", file_unique_id="s1", file_size=100, width=100, height=100)
    large = SimpleNamespace(file_id="l", file_unique_id="l1", file_size=9000, width=1920, height=1080)
    msg = SimpleNamespace(
        document=None,
        photo=[small, large],
        audio=None,
        voice=None,
        video=None,
    )
    att = extract_incoming_attachment(msg)
    assert att is not None
    assert att.file_id == "l"
    assert att.mime_type == "image/jpeg"


def test_attachment_without_caption(tmp_path):
    cfg = _cfg(tmp_path, max_download_bytes=4096)
    msg = _doc_msg(caption=None)
    incoming = extract_incoming_attachment(msg)
    dest = prepare_download_path(cfg, msg.chat_id, msg.message_id, incoming, set())

    async def _run():
        bot = AsyncMock()

        async def _get_file(_file_id):
            f = AsyncMock()
            f.download_to_drive = AsyncMock(
                side_effect=lambda **kwargs: Path(kwargs["custom_path"]).write_bytes(b"ok")
            )
            return f

        bot.get_file = _get_file
        size = await download_attachment_to_path(bot, incoming, dest, max_bytes=4096)
        downloaded = attachments.DownloadedAttachment(
            local_path=dest,
            original_name=dest.name,
            mime_type=incoming.mime_type,
            mime_confirmed=True,
            size_bytes=size,
            caption=msg.caption,
        )
        prompt = build_attachment_prompt(downloaded)
        assert "使用者說明：無" in prompt

    asyncio.run(_run())


def test_unauthorized_user_no_get_file(monkeypatch):
    monkeypatch.setenv("TGR_ALLOWED_TELEGRAM_USER_IDS", "111")
    from tg_cli_relay import telegram_bot

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        effective_chat=SimpleNamespace(id=1, type="private"),
        message=_doc_msg(),
    )
    context = SimpleNamespace()

    with patch.object(telegram_bot, "extract_incoming_attachment", return_value=MagicMock()) as ext:
        with patch.object(telegram_bot, "_download_incoming_attachment", new=AsyncMock()) as dl:
            asyncio.run(telegram_bot._on_message(update, context))
            ext.assert_not_called()
            dl.assert_not_called()


@pytest.mark.parametrize("_", [None])
def test_metadata_size_exceeded_no_download(tmp_path, _):
    from tg_cli_relay import telegram_bot

    cfg = _cfg(tmp_path, max_download_bytes=50)
    msg = _doc_msg()
    msg.document.file_size = 9999

    async def _run():
        with patch.object(telegram_bot, "download_attachment_to_path", new=AsyncMock()) as dl:
            result, err = await telegram_bot._download_incoming_attachment(msg, cfg)
            assert result is None
            assert err is not None
            assert "超過上限" in err
            dl.assert_not_called()

    asyncio.run(_run())


def test_actual_download_size_exceeded_deletes_temp(tmp_path):
    cfg = _cfg(tmp_path, max_download_bytes=5)
    incoming = IncomingAttachment("f", "u", "a.txt", "text/plain", 3, "document")
    dest = prepare_download_path(cfg, 1, 2, incoming, set())

    async def _run():
        bot = AsyncMock()

        async def _get_file(_):
            f = AsyncMock()
            f.download_to_drive = AsyncMock(
                side_effect=lambda **kwargs: Path(kwargs["custom_path"]).write_bytes(b"123456789")
            )
            return f

        bot.get_file = _get_file
        with pytest.raises(ValueError, match="超過上限"):
            await download_attachment_to_path(bot, incoming, dest, max_bytes=5)
        assert not dest.exists()
        assert not dest.with_name(f".{dest.name}.part").exists()

    asyncio.run(_run())


def test_actual_download_size_exceeded_no_backend(tmp_path, monkeypatch):
    from tg_cli_relay import telegram_bot

    monkeypatch.delenv("TGR_ALLOWED_TELEGRAM_USER_IDS", raising=False)
    monkeypatch.setenv("TGR_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("TGR_SESSION_DB", str(tmp_path / "sessions.sqlite3"))

    cfg = _cfg(tmp_path, max_download_bytes=5)
    msg = _doc_msg()
    msg.reply_text = AsyncMock()

    async def _run():
        with patch.object(telegram_bot, "_attachment_config", return_value=cfg):
            with patch.object(
                telegram_bot,
                "download_attachment_to_path",
                new=AsyncMock(side_effect=ValueError("下載檔案大小 9 超過上限 5")),
            ):
                with patch.object(telegram_bot, "relay_turn") as relay:
                    result, err = await telegram_bot._download_incoming_attachment(msg, cfg)
                    assert result is None
                    assert "超過上限" in (err or "")
                    relay.assert_not_called()

    asyncio.run(_run())


def test_malicious_filename_cannot_escape(tmp_path):
    cfg = _cfg(tmp_path)
    att = IncomingAttachment("f", "u1", "../../secret", "text/plain", 10, "document")
    dest = prepare_download_path(cfg, 1, 1, att, set())
    assert ".." not in dest.name
    assert dest.resolve().is_relative_to(cfg.upload_dir.resolve())


def test_same_name_no_overwrite(tmp_path):
    cfg = _cfg(tmp_path)
    att1 = IncomingAttachment("f1", "unique-a", "dup.txt", "text/plain", 10, "document")
    att2 = IncomingAttachment("f2", "unique-b", "dup.txt", "text/plain", 10, "document")
    existing: set[str] = set()
    p1 = prepare_download_path(cfg, 1, 1, att1, existing)
    existing.add(p1.name)
    p2 = prepare_download_path(cfg, 1, 1, att2, existing)
    assert p1.name != p2.name
    assert p1.parent == p2.parent


def test_sanitize_filename_deterministic_suffix():
    names: set[str] = set()
    first = sanitize_filename("dup.txt", mime_type="text/plain", file_unique_id="abc", existing_names=names)
    names.add(first)
    second = sanitize_filename("dup.txt", mime_type="text/plain", file_unique_id="xyz", existing_names=names)
    assert first != second


def test_symlink_escape_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    allowed = tmp_path / "uploads"
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("x")
    link = allowed / "link.txt"
    link.symlink_to(secret)

    resolved, err = validate_upload_path(str(link), cfg)
    assert resolved is None
    assert err is not None


def test_tgr_file_marker_stripped_and_valid(tmp_path):
    cfg = _cfg(tmp_path, max_upload_bytes=4096, max_files_per_message=5)
    f = cfg.upload_dir / "out.txt"
    f.write_text("hello")
    raw = f"結果如下\nTGR_FILE:{f}\n完成"
    parsed = parse_tgr_file_markers(raw)
    assert "TGR_FILE:" not in parsed.display_text
    assert parsed.marker_paths == [str(f)]
    paths, errors = resolve_outbound_files(parsed.marker_paths, cfg)
    assert errors == []
    assert paths == [f.resolve()]


def test_outbound_non_allowlist_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("nope")
    _, err = validate_upload_path(str(outside), cfg)
    assert err is not None
    assert "不在允許" in err


def test_outbound_missing_file_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    missing = cfg.upload_dir / "missing.txt"
    _, err = validate_upload_path(str(missing), cfg)
    assert err is not None


def test_outbound_directory_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    d = cfg.upload_dir / "adir"
    d.mkdir()
    _, err = validate_upload_path(str(d), cfg)
    assert err is not None


def test_outbound_fifo_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    fifo = cfg.upload_dir / "pipe"
    try:
        os.mkfifo(fifo)
    except (OSError, AttributeError):
        pytest.skip("mkfifo unavailable")
    _, err = validate_upload_path(str(fifo), cfg)
    assert err is not None


def test_outbound_count_bounded(tmp_path):
    cfg = _cfg(tmp_path, max_files_per_message=2)
    files = []
    for i in range(4):
        p = cfg.upload_dir / f"f{i}.txt"
        p.write_text(str(i))
        files.append(str(p))
    paths, errors = resolve_outbound_files(files, cfg)
    assert len(paths) <= 2
    assert any("超過單次" in e for e in errors)


def test_outbound_size_bounded(tmp_path):
    cfg = _cfg(tmp_path, max_upload_bytes=10)
    big = cfg.upload_dir / "big.bin"
    big.write_bytes(b"x" * 100)
    _, err = validate_upload_path(str(big), cfg)
    assert err is not None
    assert "大小上限" in err


def test_transient_download_retry(tmp_path):
    cfg = _cfg(tmp_path, max_download_bytes=4096)
    incoming = IncomingAttachment("f", "u", "a.txt", "text/plain", 3, "document")
    dest = prepare_download_path(cfg, 1, 2, incoming, set())
    calls = {"n": 0}

    async def _run():
        bot = AsyncMock()

        async def _get_file(_):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("timed out")
            f = AsyncMock()
            f.download_to_drive = AsyncMock(
                side_effect=lambda custom_path=None, **_: Path(custom_path).write_bytes(b"ok")
            )
            return f

        bot.get_file = _get_file
        await download_attachment_to_path(bot, incoming, dest, max_bytes=4096, delay_seconds=0)
        assert calls["n"] == 2

    asyncio.run(_run())


@pytest.mark.parametrize("_", [None])
def test_transient_upload_retry(tmp_path, _):
    from tg_cli_relay import telegram_bot

    f = tmp_path / "doc.txt"
    f.write_text("data")
    msg = SimpleNamespace(reply_document=AsyncMock(side_effect=[TimeoutError("timed out"), None]))

    async def _run():
        await telegram_bot._reply_document_with_retry(msg, f, delay_seconds=0)
        assert msg.reply_document.await_count == 2

    asyncio.run(_run())


def test_one_upload_failure_does_not_block_others(tmp_path):
    from tg_cli_relay import telegram_bot

    upload = tmp_path / "uploads"
    upload.mkdir()
    good = upload / "good.txt"
    bad = upload / "bad.txt"
    good.write_text("ok")
    bad.write_text("bad")
    cfg = AttachmentConfig(
        upload_dir=upload,
        max_download_bytes=9999,
        max_upload_bytes=9999,
        allowed_download_mime_types=frozenset({"text/plain"}),
        allowed_upload_roots=(upload.resolve(),),
        max_files_per_message=5,
        upload_retention_hours=72,
    )
    body = f"done\nTGR_FILE:{good}\nTGR_FILE:{bad}\n"
    msg = SimpleNamespace(reply_text=AsyncMock(), reply_document=AsyncMock())

    async def _doc_side_effect(**kwargs):
        if kwargs.get("filename") == "bad.txt":
            raise RuntimeError("upload fail")

    msg.reply_document.side_effect = _doc_side_effect

    async def _run():
        await telegram_bot._send_relay_output(
            msg,
            body,
            backend="codex",
            model=None,
            sid=None,
            attachment_config=cfg,
        )
        assert msg.reply_text.await_count >= 1
        assert msg.reply_document.await_count == 2
        final_calls = [
            c.args[0] if c.args else c.kwargs.get("text", "") for c in msg.reply_text.call_args_list
        ]
        assert any("bad.txt" in str(c) for c in final_calls)

    asyncio.run(_run())


def test_plain_text_message_unchanged(monkeypatch, tmp_path):
    from tg_cli_relay import telegram_bot

    monkeypatch.setenv("TGR_SESSION_DB", str(tmp_path / "sessions.sqlite3"))
    monkeypatch.setenv("TGR_DEFAULT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("TGR_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.delenv("TGR_ALLOWED_TELEGRAM_USER_IDS", raising=False)

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=1, type="private", send_message=AsyncMock()),
        message=SimpleNamespace(
            text="hello world",
            caption=None,
            document=None,
            photo=None,
            audio=None,
            voice=None,
            video=None,
            chat_id=1,
            message_id=1,
            message_thread_id=None,
            chat=SimpleNamespace(send_action=AsyncMock()),
            reply_text=AsyncMock(),
        ),
    )
    context = SimpleNamespace()

    async def _run():
        with patch.object(telegram_bot, "relay_turn", return_value=RunResult(stdout="echo", stderr="", returncode=0)):
            with patch.object(telegram_bot, "_send_relay_output", new=AsyncMock()) as send_out:
                await telegram_bot._on_message(update, context)
                telegram_bot.relay_turn.assert_called_once()
                call_kw = telegram_bot.relay_turn.call_args.kwargs
                assert call_kw["prompt"] == "hello world"
                send_out.assert_awaited_once()

    asyncio.run(_run())


def test_cleanup_bounded_no_symlink_follow(tmp_path):
    cfg = _cfg(tmp_path, upload_retention_hours=1)
    root = cfg.upload_dir
    old_dir = root / "testbot" / "1" / "10"
    old_dir.mkdir(parents=True)
    (old_dir / "f.txt").write_text("x")
    old_ts = time.time() - 7200
    os.utime(old_dir, (old_ts, old_ts))

    outside = tmp_path / "victim"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    link = root / "evil-link"
    link.symlink_to(outside)

    removed = cleanup_upload_dirs(cfg, now=time.time())
    assert removed == 1
    assert not old_dir.exists()
    assert (outside / "keep.txt").exists()


def test_cleanup_rejects_dangerous_root():
    cfg = AttachmentConfig(
        upload_dir=Path("/Users/ht"),
        max_download_bytes=1,
        max_upload_bytes=1,
        allowed_download_mime_types=frozenset({"text/plain"}),
        allowed_upload_roots=(Path("/Users/ht"),),
        max_files_per_message=1,
        upload_retention_hours=1,
    )
    assert cleanup_upload_dirs(cfg) == 0


def test_parse_tgr_file_invalid_marker():
    parsed = parse_tgr_file_markers("TGR_FILE:\nTGR_FILE: /bad path\nok")
    assert parsed.display_text == "ok"
    assert parsed.marker_errors


@pytest.mark.parametrize(
    ("mime_type", "file_name", "allowed", "expected"),
    [
        ("text/markdown", "notes.md", DEFAULT_ALLOWED_DOWNLOAD_MIME_TYPES, True),
        ("application/octet-stream", "notes.md", DEFAULT_ALLOWED_DOWNLOAD_MIME_TYPES, True),
        ("application/octet-stream", "notes.exe", DEFAULT_ALLOWED_DOWNLOAD_MIME_TYPES, False),
        ("application/x-msdownload", "notes.md", DEFAULT_ALLOWED_DOWNLOAD_MIME_TYPES, False),
    ],
)
def test_incoming_attachment_mime_policy(mime_type, file_name, allowed, expected):
    assert (
        is_incoming_attachment_allowed(mime_type, file_name, allowed) is expected
    )


def test_md_text_markdown_download_and_prompt(tmp_path):
    cfg = _cfg(
        tmp_path,
        max_download_bytes=4096,
        allowed_download_mime_types=DEFAULT_ALLOWED_DOWNLOAD_MIME_TYPES,
    )
    msg = _doc_msg(
        document=SimpleNamespace(
            file_id="fid-md",
            file_unique_id="uniq-md",
            file_name="handoff.md",
            mime_type="text/markdown",
            file_size=50,
        )
    )
    incoming = extract_incoming_attachment(msg)
    assert incoming is not None
    dest = prepare_download_path(cfg, msg.chat_id, msg.message_id, incoming, set())

    async def _run():
        bot = AsyncMock()

        async def _get_file(file_id):
            assert file_id == "fid-md"
            f = AsyncMock()
            f.download_to_drive = AsyncMock(
                side_effect=lambda **kwargs: Path(kwargs["custom_path"]).write_text("# title\n", encoding="utf-8")
            )
            return f

        bot.get_file = _get_file
        size = await download_attachment_to_path(bot, incoming, dest, max_bytes=4096)
        downloaded = attachments.DownloadedAttachment(
            local_path=dest,
            original_name=incoming.original_name or dest.name,
            mime_type=incoming.mime_type,
            mime_confirmed=is_attachment_mime_confirmed(
                incoming.mime_type, incoming.original_name, cfg.allowed_download_mime_types
            ),
            size_bytes=size,
            caption=msg.caption,
        )
        prompt = build_attachment_prompt(downloaded)
        assert "handoff.md" in prompt
        assert "text/markdown" in prompt
        assert "類型未確認" not in prompt

    asyncio.run(_run())


def test_md_octet_stream_download_prompt_mime_unconfirmed(tmp_path):
    from tg_cli_relay import telegram_bot

    cfg = _cfg(
        tmp_path,
        max_download_bytes=4096,
        allowed_download_mime_types=DEFAULT_ALLOWED_DOWNLOAD_MIME_TYPES,
    )
    msg = _doc_msg(
        document=SimpleNamespace(
            file_id="fid-oct",
            file_unique_id="uniq-oct",
            file_name="notes.md",
            mime_type="application/octet-stream",
            file_size=40,
        )
    )

    async def _run():
        with patch.object(
            telegram_bot,
            "download_attachment_to_path",
            new=AsyncMock(return_value=12),
        ):
            with patch.object(telegram_bot, "prepare_download_path") as prep:
                dest = tmp_path / "uploads" / "testbot" / "42" / "99" / "notes.md"
                dest.parent.mkdir(parents=True, exist_ok=True)
                prep.return_value = dest
                result, err = await telegram_bot._download_incoming_attachment(msg, cfg)
                assert err is None
                assert result is not None
                assert result.mime_type == "application/octet-stream"
                assert result.mime_confirmed is False
                prompt = build_attachment_prompt(result)
                assert "類型未確認" in prompt

    asyncio.run(_run())
