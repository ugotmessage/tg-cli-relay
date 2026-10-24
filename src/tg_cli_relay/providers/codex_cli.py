from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from typing import Callable

from tg_cli_relay.providers.base import RunResult
from tg_cli_relay.providers.process_control import interruptible_run, tracked_process, _terminate

# 完整清單執行 `codex models status` 或查官方文件
CODEX_MODELS: list[str] = [
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
    "gpt-oss-20b",
    "gpt-oss-120b",
]


@dataclass(slots=True)
class CodexCliProvider:
    """包裝本機 `codex exec` / `codex exec resume`。"""

    codex_bin: str = "codex"
    dangerously_bypass_approvals_and_sandbox: bool = False
    model: str | None = None
    timeout_seconds: float = 600
    name: str = "codex"

    def run_turn(
        self,
        *,
        workspace: str,
        session_id: str | None,
        prompt: str,
        on_progress: Callable[[str], None] | None = None,
        thread_key: str | None = None,
    ) -> RunResult:
        base: list[str] = [self.codex_bin]
        if self.model:
            base.extend(["-m", self.model])
        exec_args: list[str] = [
            "exec",
            "-C",
            workspace,
            "--json",
            "--skip-git-repo-check",
        ]
        if self.dangerously_bypass_approvals_and_sandbox:
            exec_args.append("--dangerously-bypass-approvals-and-sandbox")
        if session_id:
            cmd: list[str] = base + exec_args + ["resume", session_id, prompt]
        else:
            cmd = base + exec_args + [prompt]
        timeout = self.timeout_seconds if self.timeout_seconds > 0 else None
        if on_progress is None:
            try:
                proc = (interruptible_run(
                    cmd, thread_key=thread_key, env=os.environ.copy(), timeout=timeout,
                ) if thread_key is not None else subprocess.run(
                    cmd, capture_output=True, text=True, check=False,
                    env=os.environ.copy(), timeout=timeout,
                ))
                raw_stdout, raw_stderr, returncode = proc.stdout, proc.stderr, proc.returncode
            except subprocess.TimeoutExpired as exc:
                raw_stdout, raw_stderr, returncode = exc.stdout or "", exc.stderr or "", 124
                if isinstance(raw_stdout, bytes):
                    raw_stdout = raw_stdout.decode("utf-8", "replace")
                if isinstance(raw_stderr, bytes):
                    raw_stderr = raw_stderr.decode("utf-8", "replace")
                raw_stderr = (raw_stderr + "\nCodex CLI turn exceeded timeout").strip()
            messages = parse_messages_from_jsonl(raw_stdout)
        else:
            raw_lines: list[str] = []
            messages: list[str] = []
            pending: str | None = None
            with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr_file:
                opts = {"start_new_session": True} if thread_key and os.name == "posix" else {}
                if thread_key and os.name == "nt":
                    opts["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                proc_live = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=stderr_file,
                    text=True, env=os.environ.copy(), **opts,
                )
                timed_out = threading.Event()

                def stop_on_deadline() -> None:
                    if proc_live.poll() is None:
                        timed_out.set()
                        _terminate(proc_live)

                watchdog = threading.Timer(timeout, stop_on_deadline) if timeout else None
                if watchdog is not None:
                    watchdog.daemon = True
                try:
                    with tracked_process(thread_key, proc_live):
                        if watchdog is not None:
                            watchdog.start()
                        assert proc_live.stdout is not None
                        for line in proc_live.stdout:
                            raw_lines.append(line)
                            message = parse_agent_message_line(line)
                            if message is None:
                                continue
                            if pending is not None:
                                on_progress(pending)
                            pending = message
                        returncode = proc_live.wait()
                finally:
                    if watchdog is not None:
                        watchdog.cancel()
                stderr_file.seek(0)
                raw_stderr = stderr_file.read()
                if timed_out.is_set():
                    returncode = 124
                    raw_stderr = (raw_stderr + "\nCodex CLI turn exceeded timeout").strip()
            raw_stdout = "".join(raw_lines)
            if pending is not None:
                messages.append(pending)
        session_id_from_jsonl = parse_session_id_from_jsonl(raw_stdout)
        text = "\n".join(messages) if messages else None
        stdout = text if text is not None else raw_stdout
        segments = messages if len(messages) > 1 else None
        return RunResult(
            stdout=stdout,
            stderr=raw_stderr,
            returncode=returncode,
            session_id=session_id_from_jsonl,
            stdout_segments=segments,
        )


def parse_agent_message_line(line: str) -> str | None:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "item.completed":
        return None
    item = obj.get("item", {})
    if not isinstance(item, dict) or item.get("type") != "agent_message":
        return None
    text = item.get("text")
    return text if isinstance(text, str) and text else None


def parse_messages_from_jsonl(blob: str) -> list[str]:
    """從 `codex exec --json` 的 JSONL stdout 提取各段 agent_message 文字。"""
    parts: list[str] = []
    for line in blob.splitlines():
        line = line.strip()
        if not line:
            continue
        text = parse_agent_message_line(line)
        if text:
            parts.append(text)
    return parts


def parse_text_from_jsonl(blob: str) -> str | None:
    """從 `codex exec --json` 的 JSONL stdout 提取 agent 回覆文字（合併版）。"""
    messages = parse_messages_from_jsonl(blob)
    return "\n".join(messages) if messages else None


def parse_session_id_from_jsonl(blob: str) -> str | None:
    """從 `codex exec --json` 的 stdout 找出 thread id（用於 resume）。"""
    for line in blob.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "thread.started":
            val = obj.get("thread_id")
            if isinstance(val, str) and val:
                return val
        for key in ("session_id", "sessionId", "conversation_id", "conversationId"):
            val = obj.get(key)
            if isinstance(val, str) and val:
                return val
    return None
