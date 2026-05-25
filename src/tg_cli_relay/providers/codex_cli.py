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
        exec_args: list[str] = ["exec", "-C", workspace, "--json"]
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
        return RunResult(stdout=proc.stdout, stderr=proc.stderr, returncode=proc.returncode)


def parse_session_id_from_jsonl(blob: str) -> str | None:
    """從 `codex exec --json` 的 stdout 嘗試找出 session / conversation id。"""
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
        for key in (
            "session_id",
            "sessionId",
            "conversation_id",
            "conversationId",
            "id",
        ):
            val = obj.get(key)
            if isinstance(val, str) and val:
                return val
    return None
