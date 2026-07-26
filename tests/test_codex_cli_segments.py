from subprocess import CompletedProcess
from unittest.mock import AsyncMock, patch

from tg_cli_relay import telegram_bot
from tg_cli_relay.providers.codex_cli import (
    CodexCliProvider,
    parse_messages_from_jsonl,
    parse_text_from_jsonl,
)


def test_parse_messages_from_jsonl_collects_multiple_agent_messages() -> None:
    jsonl = "\n".join(
        [
            '{"type":"item.completed","item":{"type":"agent_message","text":"第一段"}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"第二段"}}',
            '{"type":"item.completed","item":{"type":"other","text":"忽略"}}',
        ]
    )
    assert parse_messages_from_jsonl(jsonl) == ["第一段", "第二段"]
    assert parse_text_from_jsonl(jsonl) == "第一段\n第二段"


def test_codex_provider_sets_stdout_segments_for_multiple_messages() -> None:
    jsonl = "\n".join(
        [
            '{"type":"thread.started","thread_id":"thread-abc"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"步驟一完成"}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"步驟二完成"}}',
        ]
    )
    completed = CompletedProcess(args=[], returncode=0, stdout=jsonl, stderr="")

    with patch("tg_cli_relay.providers.codex_cli.subprocess.run", return_value=completed):
        result = CodexCliProvider(codex_bin="codex").run_turn(
            workspace="/tmp/workspace",
            session_id=None,
            prompt="請處理",
        )

    assert result.stdout == "步驟一完成\n步驟二完成"
    assert result.stdout_segments == ["步驟一完成", "步驟二完成"]
    assert result.session_id == "thread-abc"


def test_codex_provider_omits_stdout_segments_for_single_message() -> None:
    jsonl = '{"type":"item.completed","item":{"type":"agent_message","text":"只有一段"}}'
    completed = CompletedProcess(args=[], returncode=0, stdout=jsonl, stderr="")

    with patch("tg_cli_relay.providers.codex_cli.subprocess.run", return_value=completed):
        result = CodexCliProvider(codex_bin="codex").run_turn(
            workspace="/tmp/workspace",
            session_id=None,
            prompt="請處理",
        )

    assert result.stdout == "只有一段"
    assert result.stdout_segments is None


def test_send_relay_output_sends_multiple_messages_for_codex_segments() -> None:
    msg = AsyncMock()
    segments = ["第一段回覆", "第二段回覆"]

    import asyncio

    async def _run() -> None:
        await telegram_bot._send_relay_output(
            msg,
            body="第一段回覆\n第二段回覆",
            backend="codex",
            model="gpt-5.3-codex",
            sid="thread-1",
            attachment_config=telegram_bot._attachment_config(),
            segments=segments,
        )

    with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token-for-fingerprint"}):
        asyncio.run(_run())

    assert msg.reply_text.await_count == 2
    first_call = msg.reply_text.call_args_list[0].args[0]
    second_call = msg.reply_text.call_args_list[1].args[0]
    assert first_call == "第一段回覆"
    assert second_call.startswith("第二段回覆")
    assert "— codex" in second_call
    assert "session:thread-1" in second_call


def test_chunk_segments_does_not_merge_across_logical_segments() -> None:
    long_first = "A" * telegram_bot.TG_CHUNK
    second = "短第二段"
    chunks = telegram_bot._chunk_segments([long_first, second])
    assert len(chunks) == 2
    assert chunks[0] == long_first
    assert chunks[1] == second
