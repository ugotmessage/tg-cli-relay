from __future__ import annotations

import json
import os
import re
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
        env = os.environ.copy()
        # TG relay 固定走 Claude Code 訂閱／Keychain OAuth（claude --print），不吃 API key。
        # 全域 launchctl setenv（例如 ai.openclaw.setenv-ai-keys）可能注入 sk-ant-oat01-…（OAuth token
        # 誤當 API key），若留著會變成 HTTP API 路線並出現 Invalid API key。
        api_key = env.get("ANTHROPIC_API_KEY", "")
        if not api_key or api_key.startswith("oat01-") or api_key.startswith("sk-ant-oat01-"):
            env.pop("ANTHROPIC_API_KEY", None)

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


_BG_PROC_PREFIX_RE = re.compile(
    r"^\[Background process proc_[a-f0-9]+ finished with exit code \d+\]?~?\s*",
    re.IGNORECASE,
)
_BG_FINAL_OUTPUT_RE = re.compile(r"Here's the final output:\s*", re.IGNORECASE)

_JSON_NOISE_MARKERS = (
    '"modelUsage"',
    '"terminal_reason"',
    '"total_cost_usd"',
    '"cache_read_input_tokens"',
    '"permission_denials"',
    '"subagent_type"',
    '"parent_tool_use_id"',
    '"server_tool_use"',
)


def _looks_like_json_noise(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    if t.startswith("{") and any(marker in t for marker in _JSON_NOISE_MARKERS):
        return True
    if any(marker in t for marker in _JSON_NOISE_MARKERS) and (
        t.startswith('"') or '":{' in t[:120] or t.startswith("tool_use")
    ):
        return True
    return False


def _sanitize_claude_display_text(text: str) -> str:
    t = text.strip()
    if not t or _looks_like_json_noise(t):
        return ""

    if t.startswith("[Background process proc_"):
        t = _BG_PROC_PREFIX_RE.sub("", t, count=1)
        t = _BG_FINAL_OUTPUT_RE.sub("", t, count=1).strip()
        if not t or _looks_like_json_noise(t):
            return ""

    return t


def _try_parse_json_object(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass

    start = line.find("{")
    if start < 0:
        return None
    try:
        obj = json.loads(line[start:])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _extract_claude_session_id(obj: dict) -> str | None:
    sid_raw = obj.get("session_id") or obj.get("sessionId")
    return str(sid_raw) if sid_raw else None


def _extract_claude_display_text(obj: dict) -> str | None:
    """從單一 Claude JSON 事件取出可顯示給使用者的文字。"""
    if not isinstance(obj, dict):
        return None

    typ = obj.get("type")
    if typ == "result":
        result = obj.get("result")
        if isinstance(result, str) and result.strip():
            return result.strip()
        return None

    # 單一 JSON 結果（舊版 --output-format json）
    for key in ("result", "content", "text"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    # stream-json：assistant 訊息
    if typ == "assistant":
        message = obj.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        t = block.get("text", "")
                        if isinstance(t, str) and t.strip():
                            parts.append(t.strip())
                if parts:
                    return "\n".join(parts)

    return None


def parse_claude_output(blob: str | None) -> tuple[str, str | None]:
    """解析 `claude --print --output-format json` 的輸出。

    新版 Claude Code 在 subagent / stream 情境可能輸出多行 JSONL；
    僅提取最終 `result` 或 assistant 文字，不把內部事件轉發到 TG。

    Returns:
        (display_text, session_id)
    """
    if not blob:
        return "", None
    raw = blob.strip()
    if not raw:
        return "", None

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    session_id: str | None = None
    display_parts: list[str] = []

    def ingest(obj: dict) -> None:
        nonlocal session_id
        sid = _extract_claude_session_id(obj)
        if sid:
            session_id = sid
        text = _extract_claude_display_text(obj)
        if text:
            cleaned = _sanitize_claude_display_text(text)
            if cleaned:
                display_parts.append(cleaned)

    if len(lines) == 1:
        obj = _try_parse_json_object(lines[0])
        if obj is not None:
            ingest(obj)
            if display_parts:
                return display_parts[-1], session_id

        cleaned = _sanitize_claude_display_text(raw)
        if cleaned:
            return cleaned, None
        return "", None

    for line in lines:
        obj = _try_parse_json_object(line)
        if obj is not None:
            ingest(obj)

    if display_parts:
        return display_parts[-1], session_id

    cleaned = _sanitize_claude_display_text(raw)
    if cleaned:
        return cleaned, session_id
    return "", session_id
