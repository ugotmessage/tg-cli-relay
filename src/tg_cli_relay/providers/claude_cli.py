from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

from tg_cli_relay.providers.base import RunResult

CLAUDE_MODELS: list[str] = [
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
]


@dataclass(slots=True)
class ClaudeCliProvider:
    """包裝本機 `claude` CLI（Claude Code）。

    dangerously_skip_permissions=True 會加上 --dangerously-skip-permissions，
    讓 CLI 無人值守運作，不彈出工具授權提示。
    """

    claude_bin: str = "claude"
    dangerously_skip_permissions: bool = False
    model: str | None = None
    name: str = "claude"

    def run_turn(
        self,
        *,
        workspace: str,
        session_id: str | None,
        prompt: str,
    ) -> RunResult:
        cmd: list[str] = [self.claude_bin, "--print", "--output-format", "json"]
        if self.dangerously_skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if self.model:
            cmd.extend(["--model", self.model])
        if session_id:
            cmd.extend(["--resume", session_id])
        cmd.append(prompt)
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            cwd=workspace,
            env=os.environ.copy(),
        )
        return RunResult(stdout=proc.stdout, stderr=proc.stderr, returncode=proc.returncode)


def parse_claude_output(blob: str) -> tuple[str, str | None]:
    """解析 `claude --output-format json` 的輸出。

    Returns:
        (display_text, session_id)
        display_text：要顯示給使用者的回覆文字。
        session_id：若解析成功則為 str，否則為 None。
    """
    raw = blob.strip()
    if not raw:
        return "", None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return raw, None
    if not isinstance(obj, dict):
        return raw, None

    text = obj.get("result") or obj.get("content") or obj.get("text") or ""
    sid_raw = obj.get("session_id") or obj.get("sessionId")
    session_id = str(sid_raw) if sid_raw else None
    return str(text), session_id
