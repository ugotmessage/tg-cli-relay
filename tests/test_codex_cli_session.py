from subprocess import CompletedProcess
from unittest.mock import patch

from tg_cli_relay.providers.codex_cli import CodexCliProvider


def test_codex_provider_preserves_thread_id_after_extracting_reply() -> None:
    jsonl = "\n".join(
        [
            '{"type":"thread.started","thread_id":"thread-123"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"完成了"}}',
        ]
    )
    completed = CompletedProcess(args=[], returncode=0, stdout=jsonl, stderr="")

    with patch("tg_cli_relay.providers.codex_cli.subprocess.run", return_value=completed) as run:
        result = CodexCliProvider(codex_bin="codex").run_turn(
            workspace="/tmp/workspace",
            session_id=None,
            prompt="請處理",
        )

    assert result.stdout == "完成了"
    assert result.session_id == "thread-123"
    assert run.call_args.args[0] == [
        "codex",
        "exec",
        "-C",
        "/tmp/workspace",
        "--json",
        "--skip-git-repo-check",
        "請處理",
    ]


def test_codex_provider_resumes_saved_thread() -> None:
    completed = CompletedProcess(
        args=[],
        returncode=0,
        stdout='{"type":"item.completed","item":{"type":"agent_message","text":"延續"}}',
        stderr="",
    )

    with patch("tg_cli_relay.providers.codex_cli.subprocess.run", return_value=completed) as run:
        CodexCliProvider(codex_bin="codex").run_turn(
            workspace="/tmp/workspace",
            session_id="thread-123",
            prompt="繼續",
        )

    assert run.call_args.args[0] == [
        "codex",
        "exec",
        "-C",
        "/tmp/workspace",
        "--json",
        "--skip-git-repo-check",
        "resume",
        "thread-123",
        "繼續",
    ]
