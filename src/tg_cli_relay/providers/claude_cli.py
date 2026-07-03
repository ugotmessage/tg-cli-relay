from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

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
    run_as_user: str | None = None
    model: str | None = None
    timeout_seconds: int = 1800
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
        knowledge_dir = os.environ.get("TGR_KNOWLEDGE_DIR", "/srv/docker/knowledge").strip()
        if knowledge_dir and Path(knowledge_dir).is_dir():
            cmd.extend(["--add-dir", knowledge_dir])
        env = os.environ.copy()
        # TG relay 固定走 Claude Code 訂閱／Keychain OAuth（claude --print），不吃 API key。
        # 全域 launchctl setenv（例如 ai.openclaw.setenv-ai-keys）可能注入 sk-ant-oat01-…（OAuth token
        # 誤當 API key），若留著會變成 HTTP API 路線並出現 Invalid API key。
        api_key = env.get("ANTHROPIC_API_KEY", "")
        if not api_key or api_key.startswith("oat01-") or api_key.startswith("sk-ant-oat01-"):
            env.pop("ANTHROPIC_API_KEY", None)

        if self.run_as_user and os.geteuid() == 0:
            for key in ("HOME", "USER", "LOGNAME", "SUDO_USER"):
                env.pop(key, None)
            cmd = ["sudo", "-n", "-u", self.run_as_user, "-H", "--", *cmd]

        timeout = self.timeout_seconds if self.timeout_seconds > 0 else None
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                check=False,
                cwd=workspace,
                env=env,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            err = (exc.stderr or "").strip()
            msg = f"claude 執行逾時（>{self.timeout_seconds} 秒）"
            return RunResult(
                stdout=exc.stdout or "",
                stderr=f"{err}\n{msg}".strip() if err else msg,
                returncode=124,
            )
        return RunResult(stdout=proc.stdout or "", stderr=proc.stderr or "", returncode=proc.returncode)


def parse_claude_output(blob: str | None) -> tuple[str, str | None]:
    """解析 `claude --output-format json` 的輸出。

    Returns:
        (display_text, session_id)
        display_text：要顯示給使用者的回覆文字。
        session_id：若解析成功則為 str，否則為 None。
    """
    if not blob:
        return "", None
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
