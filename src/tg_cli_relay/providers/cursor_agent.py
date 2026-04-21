from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from tg_cli_relay.providers.base import RunResult


@dataclass(slots=True)
class CursorAgentProvider:
    """包裝本機 `agent`（Cursor Agent CLI）。"""

    agent_bin: str = "agent"
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
        if session_id:
            cmd.extend(["--resume", session_id])
        cmd.append(prompt)
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=os.environ.copy(),
        )
        return RunResult(stdout=proc.stdout, stderr=proc.stderr, returncode=proc.returncode)
