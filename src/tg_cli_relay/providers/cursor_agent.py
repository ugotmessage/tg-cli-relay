from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass

from tg_cli_relay.providers.base import RunResult

# Cursor CLI 會把最後一次 --model 寫入全域 cli-config；relay 預設必須顯式傳 auto，避免被其它 agent 呼叫污染。
CURSOR_DEFAULT_MODEL = "auto"

# 精選常用模型；完整清單請執行 `agent --list-models`
CURSOR_MODELS: list[str] = [
    "auto",
    "composer-2-fast",
    "composer-2",
    "claude-opus-4-7-xhigh",
    "claude-4.6-sonnet-medium",
    "gpt-5.5-medium",
    "gpt-5.5-high",
    "gpt-5.3-codex",
    "gpt-5.3-codex-high",
    "gpt-5.2",
    "gemini-3.1-pro",
    "grok-4.3",
]

_MODEL_CACHE: tuple[float, list[str]] | None = None


def list_cursor_models(agent_bin: str = "agent", *, cache_seconds: int = 3600) -> list[str]:
    """Return live Cursor model ids, with a bounded cache and curated fallback."""
    global _MODEL_CACHE

    now = time.monotonic()
    if _MODEL_CACHE is not None and now - _MODEL_CACHE[0] < cache_seconds:
        return list(_MODEL_CACHE[1])
    try:
        proc = subprocess.run(
            [agent_bin, "--list-models"],
            capture_output=True,
            text=True,
            check=False,
            env=os.environ.copy(),
            timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired):
        return list(CURSOR_MODELS)
    if proc.returncode != 0:
        return list(CURSOR_MODELS)
    models: list[str] = []
    for line in proc.stdout.splitlines():
        match = re.match(r"^([a-z0-9][a-z0-9._-]*)\s+-\s+", line.strip(), re.IGNORECASE)
        if match:
            models.append(match.group(1))
    if not models:
        return list(CURSOR_MODELS)
    _MODEL_CACHE = (now, models)
    return list(models)


@dataclass(slots=True)
class CursorAgentProvider:
    """包裝本機 `agent`（Cursor Agent CLI）。"""

    agent_bin: str = "agent"
    model: str | None = None
    timeout_seconds: int = 600
    name: str = "cursor"

    def create_session(self) -> str:
        proc = subprocess.run(
            [self.agent_bin, "create-chat"],
            capture_output=True,
            text=True,
            check=False,
            env=os.environ.copy(),
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "create-chat failed")
        sid = proc.stdout.strip()
        if not sid:
            raise RuntimeError("create-chat returned empty session id")
        return sid

    def run_turn(
        self,
        *,
        workspace: str,
        session_id: str | None,
        prompt: str,
    ) -> RunResult:
        cmd: list[str] = [
            self.agent_bin,
            "--print",
            "--trust",
            "--workspace",
            workspace,
        ]
        cmd.extend(["--model", self.model or CURSOR_DEFAULT_MODEL])
        if session_id:
            cmd.extend(["--resume", session_id])
        cmd.append(prompt)
        timeout = self.timeout_seconds if self.timeout_seconds > 0 else None
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                env=os.environ.copy(),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            err = (exc.stderr or "").strip()
            msg = f"agent 執行逾時（>{self.timeout_seconds} 秒）"
            return RunResult(
                stdout=exc.stdout or "",
                stderr=f"{err}\n{msg}".strip() if err else msg,
                returncode=124,
            )
        return RunResult(stdout=proc.stdout, stderr=proc.stderr, returncode=proc.returncode)
