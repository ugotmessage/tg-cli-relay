from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True)
class RunResult:
    stdout: str
    stderr: str
    returncode: int


class CliProvider(Protocol):
    """可插拔後端：Cursor agent、Codex exec 等。"""

    name: str

    def run_turn(
        self,
        *,
        workspace: str,
        session_id: str | None,
        prompt: str,
    ) -> RunResult: ...
