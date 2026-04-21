from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

from tg_cli_relay.providers.base import RunResult


@dataclass(slots=True)
class CodexCliProvider:
    """包裝本機 `codex exec` / `codex exec resume`。"""

    codex_bin: str = "codex"
    name: str = "codex"

    def run_turn(
        self,
        *,
        workspace: str,
        session_id: str | None,
        prompt: str,
    ) -> RunResult:
        if session_id:
            cmd: list[str] = [
                self.codex_bin,
                "exec",
                "-C",
                workspace,
                "--json",
                "resume",
                session_id,
                prompt,
            ]
        else:
            cmd = [self.codex_bin, "exec", "-C", workspace, "--json", prompt]
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
