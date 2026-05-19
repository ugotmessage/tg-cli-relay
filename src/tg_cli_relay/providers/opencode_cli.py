from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

from tg_cli_relay.providers.base import RunResult

# 格式為 provider/model-name；完整清單參考 opencode 官方文件
OPENCODE_MODELS: list[str] = [
    "anthropic/claude-sonnet-4-6",
    "anthropic/claude-opus-4-7",
    "anthropic/claude-haiku-4-5",
    "openai/gpt-5.5",
    "openai/gpt-5.4",
    "openai/gpt-5.4-mini",
    "openai/o3",
    "google/gemini-3.1-pro",
    "google/gemini-2.5-pro",
]


@dataclass(slots=True)
class OpencodeCliProvider:
    """包裝本機 `opencode run`（OpenCode CLI）。

    OpenCode 無 --resume <id>，只有 -c（繼續最後一個 session）。
    多 thread 共用同一 workspace 時 session 無法隔離，建議一個 workspace 對應一個 thread。
    """

    opencode_bin: str = "opencode"
    model: str | None = None
    name: str = "opencode"

    def run_turn(
        self,
        *,
        workspace: str,
        session_id: str | None,
        prompt: str,
    ) -> RunResult:
        cmd: list[str] = [self.opencode_bin, "run", "--format", "json"]
        if self.model:
            cmd.extend(["-m", self.model])
        if session_id:
            cmd.append("-c")  # 有過 session 就接續（最後一個）
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


def parse_opencode_output(blob: str) -> tuple[str, str | None]:
    """解析 `opencode run --format json` 的 JSONL 輸出。

    Returns:
        (display_text, session_id)
    """
    texts: list[str] = []
    session_id: str | None = None

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

        # session ID：嘗試常見欄位名稱
        for key in ("sessionID", "session_id", "sessionId", "id"):
            val = obj.get(key)
            if isinstance(val, str) and val and not session_id:
                session_id = val
                break

        # 文字內容：支援多種 event 結構
        # 直接 content/text 欄位
        for key in ("content", "text", "result"):
            val = obj.get(key)
            if isinstance(val, str) and val:
                texts.append(val)
                break
        else:
            # 巢狀結構：message.parts[].text
            msg = obj.get("message", {})
            if isinstance(msg, dict):
                for part in msg.get("parts", []):
                    if isinstance(part, dict) and part.get("type") == "text":
                        t = part.get("text", "")
                        if t:
                            texts.append(t)

    display_text = "\n".join(texts) if texts else blob.strip()
    return display_text, session_id
