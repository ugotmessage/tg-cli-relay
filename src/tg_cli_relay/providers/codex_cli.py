from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

from tg_cli_relay.providers.base import RunResult

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
    name: str = "codex"

    def run_turn(
        self,
        *,
        workspace: str,
        session_id: str | None,
        prompt: str,
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
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=os.environ.copy(),
        )
        session_id_from_jsonl = parse_session_id_from_jsonl(proc.stdout)
        messages = parse_messages_from_jsonl(proc.stdout)
        text = "\n".join(messages) if messages else None
        stdout = text if text is not None else proc.stdout
        segments = messages if len(messages) > 1 else None
        return RunResult(
            stdout=stdout,
            stderr=proc.stderr,
            returncode=proc.returncode,
            session_id=session_id_from_jsonl,
            stdout_segments=segments,
        )


def parse_messages_from_jsonl(blob: str) -> list[str]:
    """從 `codex exec --json` 的 JSONL stdout 提取各段 agent_message 文字。"""
    parts: list[str] = []
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
        if obj.get("type") == "item.completed":
            item = obj.get("item", {})
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text", "")
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
