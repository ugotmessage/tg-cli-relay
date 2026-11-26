from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from tg_cli_relay.providers.base import RunResult
from tg_cli_relay.providers.process_control import _terminate, interruptible_run, tracked_process

ANTIGRAVITY_DEFAULT_MODEL = "gemini-4-hydrogen-medium"

# Curated fallback list matching standalone `agy models`; live catalog comes from `agy models`.
ANTIGRAVITY_MODELS: list[str] = [
    "gemini-4-hydrogen-medium",
    "gemini-4-hydrogen-high",
    "gemini-4-hydrogen-xhigh",
    "gemini-4-hydrogen-low",
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-low",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium",
]

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_MODEL_LINE_RE = re.compile(r"^([a-z0-9][a-z0-9._-]*)\s{2,}\S", re.IGNORECASE)
_MODEL_CACHE: tuple[float, list[str]] | None = None


def parse_antigravity_models_output(raw: str) -> list[str]:
    """Parse `agy models` stdout into model ids, tolerating spinner/CR prefixes."""
    models: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        # Spinner frames use '\r' on the first line before the actual model row.
        segment = line.split("\r")[-1]
        cleaned = _ANSI_ESCAPE_RE.sub("", segment).strip()
        if not cleaned:
            continue
        match = _MODEL_LINE_RE.match(cleaned)
        if not match:
            continue
        model_id = match.group(1)
        if model_id not in seen:
            seen.add(model_id)
            models.append(model_id)
    return models


def list_antigravity_models(
    agy_bin: str = "agy", *, cache_seconds: int = 3600
) -> list[str]:
    """Return live Antigravity model ids with bounded cache and curated fallback."""
    global _MODEL_CACHE

    now = time.monotonic()
    if _MODEL_CACHE is not None and now - _MODEL_CACHE[0] < cache_seconds:
        return list(_MODEL_CACHE[1])
    try:
        proc = subprocess.run(
            [agy_bin, "models"],
            capture_output=True,
            text=True,
            check=False,
            env=os.environ.copy(),
            timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired):
        return list(ANTIGRAVITY_MODELS)
    if proc.returncode != 0:
        return list(ANTIGRAVITY_MODELS)
    models = parse_antigravity_models_output(proc.stdout or "")
    if not models:
        return list(ANTIGRAVITY_MODELS)
    _MODEL_CACHE = (now, models)
    return list(models)


@dataclass(slots=True)
class AntigravityCliProvider:
    """Wrap local `agy` (Antigravity CLI) in print mode (`--output-format json|stream-json`)."""

    agy_bin: str = "agy"
    dangerously_skip_permissions: bool = False
    sandbox: bool = False
    model: str | None = None
    timeout_seconds: float = 600
    add_dirs: tuple[str, ...] = ()
    name: str = "antigravity"

    def _build_cmd(
        self,
        *,
        session_id: str | None,
        prompt: str,
        output_format: str,
    ) -> list[str]:
        cmd: list[str] = [self.agy_bin, "--output-format", output_format]
        if self.dangerously_skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if self.sandbox:
            cmd.append("--sandbox")
        if self.model:
            cmd.extend(["--model", self.model])
        if session_id:
            cmd.extend(["--resume", session_id])
        for directory in self.add_dirs:
            d = directory.strip()
            if d and Path(d).is_dir():
                cmd.extend(["--add-dir", d])
        # `--print` is a value flag on `agy`; attach with `=` so prompts starting
        # with `-` are never misparsed as CLI flags.
        cmd.append(f"--print={prompt}")
        return cmd

    def run_turn(
        self,
        *,
        workspace: str,
        session_id: str | None,
        prompt: str,
        on_progress: Callable[[str], None] | None = None,
        thread_key: str | None = None,
        on_handoff: Callable[[str, str, str], bool] | None = None,
    ) -> RunResult:
        stream = on_progress is not None or on_handoff is not None
        output_format = "stream-json" if stream else "json"
        cmd = self._build_cmd(
            session_id=session_id,
            prompt=prompt,
            output_format=output_format,
        )
        env = os.environ.copy()
        timeout = self.timeout_seconds if self.timeout_seconds > 0 else None

        if stream:
            return self._run_streaming(
                cmd=cmd,
                workspace=workspace,
                env=env,
                timeout=timeout,
                session_id=session_id,
                thread_key=thread_key,
                on_progress=on_progress,
                on_handoff=on_handoff,
            )

        try:
            proc = (
                interruptible_run(
                    cmd,
                    thread_key=thread_key,
                    cwd=workspace,
                    env=env,
                    timeout=timeout,
                )
                if thread_key is not None
                else subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                    cwd=workspace,
                    env=env,
                    timeout=timeout,
                )
            )
        except subprocess.TimeoutExpired as exc:
            err = exc.stderr or ""
            if isinstance(err, bytes):
                err = err.decode("utf-8", "replace")
            output = exc.stdout or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", "replace")
            msg = f"agy 執行逾時（>{self.timeout_seconds} 秒）"
            return RunResult(
                stdout=output,
                stderr=f"{err}\n{msg}".strip() if err else msg,
                returncode=124,
            )

        raw_stdout = proc.stdout or ""
        raw_stderr = proc.stderr or ""
        returncode = proc.returncode
        display, parsed_sid, is_error = parse_antigravity_output(raw_stdout)
        if returncode == 0 and is_error:
            returncode = 1
        return RunResult(
            stdout=display,
            stderr=raw_stderr,
            returncode=returncode,
            session_id=parsed_sid or session_id,
        )

    def _run_streaming(
        self,
        *,
        cmd: list[str],
        workspace: str,
        env: dict[str, str],
        timeout: float | None,
        session_id: str | None,
        thread_key: str | None,
        on_progress: Callable[[str], None] | None,
        on_handoff: Callable[[str, str, str], bool] | None,
    ) -> RunResult:
        """Run `agy --output-format stream-json`, emit live progress, and detach on handoff."""
        from tg_cli_relay.background_callback import parse_handoff_marker

        raw_lines: list[str] = []
        step_buffers: dict[int, list[str]] = {}
        completed_unsent: list[str] = []
        pending: str | None = None
        known_sid: str | None = session_id
        result_text: str | None = None
        result_is_error = False
        handoff_detached = False

        opts: dict = {"start_new_session": True} if os.name == "posix" else {}
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr_file:
            proc_live = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
                cwd=workspace,
                env=env,
                **opts,
            )
            timed_out = threading.Event()

            def stop_on_deadline() -> None:
                if proc_live.poll() is None:
                    timed_out.set()
                    _terminate(proc_live)

            watchdog = threading.Timer(timeout, stop_on_deadline) if timeout else None
            if watchdog is not None:
                watchdog.daemon = True
            try:
                with tracked_process(thread_key, proc_live):
                    if watchdog is not None:
                        watchdog.start()
                    assert proc_live.stdout is not None
                    for line in proc_live.stdout:
                        raw_lines.append(line)
                        obj = _try_parse_json_object(line)
                        if obj is None:
                            continue
                        sid = _extract_conversation_id(obj)
                        if sid:
                            known_sid = sid
                        event = obj.get("event")
                        if event == "result":
                            res_obj = obj.get("result")
                            if isinstance(res_obj, dict):
                                resp = res_obj.get("response")
                                result_text = resp.strip() if isinstance(resp, str) else None
                                result_is_error = _status_is_error(res_obj.get("status"))
                            continue
                        if event != "step_update":
                            continue
                        su = obj.get("step_update")
                        if not isinstance(su, dict):
                            continue
                        if su.get("step_type") != "agent_response":
                            continue
                        idx = su.get("step_index")
                        if not isinstance(idx, int):
                            continue
                        delta = su.get("text_delta")
                        if isinstance(delta, str) and delta:
                            step_buffers.setdefault(idx, []).append(delta)
                        if su.get("state") != "DONE":
                            continue
                        text = "".join(step_buffers.pop(idx, ())).strip()
                        if not text:
                            continue
                        if on_progress is not None:
                            if pending is not None:
                                on_progress(pending)
                            pending = text
                        else:
                            completed_unsent.append(text)
                        if on_handoff is None or not known_sid:
                            continue
                        parsed = parse_handoff_marker(text)
                        if parsed is None:
                            continue
                        try:
                            accepted = bool(on_handoff(parsed[0], parsed[1], known_sid))
                        except Exception:
                            accepted = False
                        if not accepted:
                            continue
                        handoff_detached = True
                        _terminate(proc_live)
                        break
                    returncode = proc_live.wait()
            finally:
                if watchdog is not None:
                    watchdog.cancel()
            stderr_file.seek(0)
            raw_stderr = stderr_file.read()

        if handoff_detached:
            returncode = 0
        elif timed_out.is_set():
            returncode = 124
            raw_stderr = (raw_stderr + f"\nagy 執行逾時（>{timeout} 秒）").strip()
        elif returncode == 0 and result_is_error:
            returncode = 1

        if on_progress is not None:
            visible = [pending] if pending else []
        else:
            visible = list(completed_unsent)

        if not handoff_detached and result_text is not None:
            if on_progress is None:
                visible = [result_text] if result_text else []
            elif not visible and result_text:
                visible = [result_text]

        if visible or result_text is not None:
            stdout = "\n".join(visible)
        else:
            # Killed (/stop, timeout) before any text: never forward raw stream-json
            # events (init, tool parameters) to Telegram; keep only plain-text lines.
            plain = [ln for ln in raw_lines if ln.strip() and _try_parse_json_object(ln) is None]
            stdout = "".join(plain).strip()[:4000]

        return RunResult(
            stdout=stdout,
            stderr=raw_stderr,
            returncode=returncode,
            session_id=known_sid,
            stdout_segments=visible if len(visible) > 1 else None,
            handoff_detached=handoff_detached,
        )


def _try_parse_json_object(line: str) -> dict | None:
    text = line.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _extract_conversation_id(obj: dict) -> str | None:
    for key in ("conversation_id", "conversationId", "session_id", "sessionId"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    for nested_key in ("result", "step_update", "init"):
        nested = obj.get(nested_key)
        if isinstance(nested, dict):
            for key in ("conversation_id", "conversationId", "session_id", "sessionId"):
                val = nested.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    return None


def _status_is_error(status: object) -> bool:
    if not isinstance(status, str) or not status.strip():
        return False
    return status.strip().upper() != "SUCCESS"


def parse_antigravity_output(blob: str | None) -> tuple[str, str | None, bool]:
    """Parse `agy --output-format json` (or stream-json fallback) output.

    Returns:
        (display_text, conversation_id, is_error)
    """
    if not blob:
        return "", None, False
    raw = blob.strip()
    if not raw:
        return "", None, False

    conversation_id: str | None = None
    result_text: str | None = None
    is_error = False
    step_buffers: dict[int, list[str]] = {}
    completed_steps: list[str] = []
    parsed_any_json = False

    for line in raw.splitlines():
        obj = _try_parse_json_object(line)
        if obj is None:
            continue
        parsed_any_json = True
        sid = _extract_conversation_id(obj)
        if sid:
            conversation_id = sid
        event = obj.get("event")
        if event == "result" and isinstance(obj.get("result"), dict):
            res_obj = obj["result"]
            if isinstance(res_obj.get("response"), str):
                result_text = res_obj["response"].strip()
            is_error = _status_is_error(res_obj.get("status"))
            continue
        if event == "step_update" and isinstance(obj.get("step_update"), dict):
            su = obj["step_update"]
            if su.get("step_type") == "agent_response" and isinstance(su.get("step_index"), int):
                idx = su["step_index"]
                delta = su.get("text_delta")
                if isinstance(delta, str) and delta:
                    step_buffers.setdefault(idx, []).append(delta)
                if su.get("state") == "DONE":
                    text = "".join(step_buffers.pop(idx, ())).strip()
                    if text:
                        completed_steps.append(text)
            continue
        # Single-object `--output-format json` shape:
        # {"conversation_id":"...","status":"SUCCESS","response":"..."}
        if isinstance(obj.get("response"), str):
            result_text = obj["response"].strip()
        if "status" in obj:
            is_error = _status_is_error(obj.get("status"))

    if result_text is not None:
        return result_text, conversation_id, is_error
    if completed_steps:
        return completed_steps[-1], conversation_id, is_error
    if parsed_any_json:
        return "", conversation_id, is_error
    return raw, None, False
