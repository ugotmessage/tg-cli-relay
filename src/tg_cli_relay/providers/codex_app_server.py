"""Minimal persistent Codex App Server transport.

Uses Codex's existing login and native threads; no secondary LLM/API key.
One subprocess is shared by worker threads, while turn notifications remain
scoped to the thread that started them. CLI transport remains the default.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import threading
from collections.abc import Callable
from typing import Any

from tg_cli_relay.providers.base import RunResult
from tg_cli_relay.providers.process_control import interrupt_requested

log = logging.getLogger(__name__)


class CodexAppServerError(RuntimeError):
    pass


class AppServerClient:
    """One async stdio JSON-RPC client; all methods execute on one event loop."""

    def __init__(self, codex_bin: str) -> None:
        self.codex_bin = codex_bin
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task[None] | None = None
        self.stderr_reader: asyncio.Task[None] | None = None
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.next_id = 0
        self.active_turns: dict[str, tuple[str, str]] = {}
        self.write_lock = asyncio.Lock()
        self.start_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self.start_lock:
            if self.process is not None and self.process.returncode is None and self.reader and not self.reader.done():
                return
            if self.process is not None:
                await self.close()
            self.process = await asyncio.create_subprocess_exec(
                self.codex_bin, "app-server", "--listen", "stdio://",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy(),
                limit=128 * 1024 * 1024,
            )
            self.reader = asyncio.create_task(self._read())
            self.stderr_reader = asyncio.create_task(self._drain_stderr())
            try:
                await asyncio.wait_for(self.request(
                    "initialize", {"clientInfo": {
                        "name": "tg-cli-relay", "title": "Telegram CLI Relay", "version": "1"
                    }}, ensure_started=False,
                ), timeout=30)
                await self.notify("initialized", {})
            except Exception:
                await self.close()
                raise

    async def close(self) -> None:
        process, self.process = self.process, None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        for task in (self.reader, self.stderr_reader):
            if task is not None and not task.done():
                task.cancel()
        self.active_turns.clear()
        self._fail_pending("App Server stopped")

    async def _send(self, payload: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise CodexAppServerError("Codex App Server is not connected")
        async with self.write_lock:
            process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await process.stdin.drain()

    async def request(
        self, method: str, params: dict[str, Any] | None = None,
        *, ensure_started: bool = True, timeout: float = 60,
    ) -> Any:
        if ensure_started:
            await self.start()
        self.next_id += 1
        rid = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        try:
            await self._send({"id": rid, "method": method, "params": params or {}})
            result = await asyncio.wait_for(future, timeout)
            return result
        finally:
            self.pending.pop(rid, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"method": method, "params": params or {}})

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.subscribers.add(inbox)
        return inbox

    def unsubscribe(self, inbox: asyncio.Queue[dict[str, Any]]) -> None:
        self.subscribers.discard(inbox)

    def _fail_pending(self, detail: str) -> None:
        for future in tuple(self.pending.values()):
            if not future.done():
                future.set_exception(CodexAppServerError(detail))
        for inbox in tuple(self.subscribers):
            inbox.put_nowait({"method": "relay/disconnected", "params": {"message": detail}})

    async def _read(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            while line := await process.stdout.readline():
                try:
                    payload = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    log.warning("Codex App Server returned a non-JSON line")
                    continue
                if not isinstance(payload, dict):
                    continue
                if "id" in payload and "method" not in payload:
                    waiter = self.pending.get(payload["id"])
                    if waiter and not waiter.done():
                        if "error" in payload:
                            error = payload["error"]
                            detail = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                            waiter.set_exception(CodexAppServerError(detail))
                        else:
                            waiter.set_result(payload.get("result"))
                elif "id" in payload and "method" in payload:
                    # This unattended Telegram bot has no interactive approval UI.
                    # Deny server-originated approval requests instead of blocking forever.
                    await self._send({"id": payload["id"], "result": {"decision": "decline"}})
                elif "method" in payload:
                    for inbox in tuple(self.subscribers):
                        inbox.put_nowait(payload)
        except Exception as exc:
            log.warning("Codex App Server reader failed: %s", exc)
        finally:
            self.active_turns.clear()
            self._fail_pending("Codex App Server disconnected")

    async def _drain_stderr(self) -> None:
        process = self.process
        if process is not None and process.stderr is not None:
            try:
                while line := await process.stderr.readline():
                    if line:
                        log.debug("Codex app-server stderr: %s", line.decode(errors="replace").rstrip()[:300])
            except asyncio.CancelledError:
                pass


class CodexAppServerProvider:
    """Synchronous adapter for relay_turn's worker threads."""

    name = "codex"

    def __init__(
        self, codex_bin: str = "codex", model: str | None = None,
        dangerously_bypass_approvals_and_sandbox: bool = False,
        timeout_seconds: float | None = None,
    ) -> None:
        self.codex_bin = codex_bin
        self.model = model
        self.bypass = dangerously_bypass_approvals_and_sandbox
        self.timeout_seconds = timeout_seconds

    def run_turn(
        self, *, workspace: str, session_id: str | None, prompt: str,
        on_progress: Callable[[str], None] | None = None,
        thread_key: str | None = None,
        on_session: Callable[[str], None] | None = None,
        on_handoff: Callable[[str, str, str], bool] | None = None,
    ) -> RunResult:
        timeout = (float(os.environ.get("TGR_CODEX_APP_TURN_TIMEOUT", "7200"))
                   if self.timeout_seconds is None else self.timeout_seconds)
        runtime = _runtime_for(self.codex_bin)
        return runtime.run(
            _run_turn(
                runtime.client, workspace=workspace, session_id=session_id,
                prompt=prompt, model=self.model, bypass=self.bypass,
                timeout=timeout, on_progress=on_progress, thread_key=thread_key,
                on_session=on_session, on_handoff=on_handoff,
            ),
            timeout=timeout + 90 if timeout > 0 else None,
        )


async def _run_turn(
    client: AppServerClient, *, workspace: str, session_id: str | None,
    prompt: str, model: str | None, bypass: bool, timeout: float,
    on_progress: Callable[[str], None] | None,
    thread_key: str | None = None,
    on_session: Callable[[str], None] | None = None,
    on_handoff: Callable[[str, str, str], bool] | None = None,
) -> RunResult:
    await client.start()
    policy = "danger-full-access" if bypass else os.environ.get("TGR_CODEX_APP_SANDBOX", "workspace-write")
    approval = "never"  # unattended Telegram; server approval prompts are declined
    if session_id:
        resumed = await client.request("thread/resume", {"threadId": session_id})
        thread_id = (resumed or {}).get("thread", {}).get("id")
        if thread_id != session_id:
            raise CodexAppServerError("Codex did not resume the expected thread")
    else:
        args: dict[str, Any] = {"cwd": workspace, "sandbox": policy, "approvalPolicy": approval}
        if model:
            args["model"] = model
        started = await client.request("thread/start", args)
        thread_id = (started or {}).get("thread", {}).get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise CodexAppServerError("thread/start did not return a thread ID")
    # Commit the provider's new thread identity before a potentially long turn.
    # This also permits resume after a process crash/interruption.
    if on_session:
        on_session(thread_id)
    inbox = client.subscribe()  # subscribe before turn/start, so no fast-completion race
    turn_id: str | None = None
    messages: list[str] = []
    pending: str | None = None
    error: str | None = None
    timed_out = False
    handoff_detached = False
    handoff_interrupt_ok = False
    try:
        args = {
            "threadId": thread_id, "cwd": workspace,
            "input": [{"type": "text", "text": prompt}],
        }
        if model:
            args["model"] = model
        started_turn = await client.request("turn/start", args)
        turn_id = (started_turn or {}).get("turn", {}).get("id")
        if not turn_id:
            raise CodexAppServerError("turn/start did not return a turn ID")
        if thread_key:
            client.active_turns[thread_key] = (thread_id, turn_id)
            # /stop can race with thread/start or turn/start; honor the latch.
            if interrupt_requested(thread_key):
                await client.request("turn/interrupt", {
                    "threadId": thread_id, "turnId": turn_id,
                }, timeout=10)
        async def receive() -> None:
            nonlocal pending, error, handoff_detached, handoff_interrupt_ok
            while True:
                event = await inbox.get()
                method = event.get("method")
                params = event.get("params") or {}
                if method == "relay/disconnected":
                    raise CodexAppServerError(str(params.get("message")))
                if params.get("threadId") != thread_id:
                    continue
                incoming_turn = params.get("turnId")
                if incoming_turn is not None and incoming_turn != turn_id:
                    continue
                if method == "item/completed":
                    item = params.get("item") or {}
                    if item.get("type") == "agentMessage":
                        content = item.get("text")
                        if isinstance(content, str) and content:
                            if pending and on_progress:
                                on_progress(pending)
                            if pending:
                                messages.append(pending)
                            pending = content
                            parsed = None
                            if on_handoff is not None:
                                from tg_cli_relay.background_callback import parse_handoff_marker

                                parsed = parse_handoff_marker(content)
                            if parsed is not None:
                                try:
                                    accepted = bool(on_handoff(parsed[0], parsed[1], thread_id))
                                except Exception:
                                    accepted = False
                                if accepted:
                                    try:
                                        await client.request(
                                            "turn/interrupt",
                                            {"threadId": thread_id, "turnId": turn_id},
                                            timeout=10,
                                        )
                                    except Exception:
                                        log.warning("Codex handoff interrupt failed")
                                        continue
                                    handoff_interrupt_ok = True
                                    continue
                if method == "error" and not params.get("willRetry", False):
                    error_obj = params.get("error") or {}
                    error = error_obj.get("message") if isinstance(error_obj, dict) else str(error_obj)
                if method == "turn/completed":
                    turn = params.get("turn") or {}
                    status = turn.get("status", "completed")
                    if handoff_interrupt_ok:
                        handoff_detached = True
                        error = None
                        return
                    if status not in ("completed", "success"):
                        err = turn.get("error") or {}
                        error = err.get("message", str(status)) if isinstance(err, dict) else str(err)
                        error = error or str(status)
                    return
        if timeout > 0:
            try:
                await asyncio.wait_for(receive(), timeout)
            except asyncio.TimeoutError:
                timed_out = True
                error = f"Codex App Server turn exceeded {timeout:g} seconds"
                try:
                    await client.request(
                        "turn/interrupt", {"threadId": thread_id, "turnId": turn_id},
                        timeout=10,
                    )
                except Exception:
                    log.warning("Codex timed out; failed to interrupt its turn")
        else:
            await receive()
    except asyncio.CancelledError:
        if turn_id:
            try:
                await client.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=10)
            except Exception:
                log.warning("Failed to interrupt cancelled Codex turn")
        raise
    finally:
        if thread_key and client.active_turns.get(thread_key) == (thread_id, turn_id):
            client.active_turns.pop(thread_key, None)
        client.unsubscribe(inbox)
    if pending:
        messages.append(pending)
    text = "\n".join(messages)
    if handoff_detached:
        returncode = 0
        error = None
    else:
        returncode = 124 if timed_out else (1 if error else 0)
    return RunResult(
        stdout=text, stderr=error or "", returncode=returncode,
        session_id=thread_id, stdout_segments=messages if len(messages) > 1 else None,
        handoff_detached=handoff_detached,
    )


async def _steer_active_turn(client: AppServerClient, thread_key: str, prompt: str) -> bool:
    """A message can steer only an already running, matching logical parent."""
    active = client.active_turns.get(thread_key)
    if active is None:
        return False
    thread_id, turn_id = active
    try:
        await client.request(
            "turn/steer", {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": prompt}],
            }, timeout=8,
        )
        return True
    except (CodexAppServerError, asyncio.TimeoutError):
        return False


async def _interrupt_active_turn(client: AppServerClient, thread_key: str) -> bool:
    active = client.active_turns.get(thread_key)
    if active is None:
        return False
    thread_id, turn_id = active
    try:
        await client.request(
            "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=8,
        )
        return True
    except (CodexAppServerError, asyncio.TimeoutError):
        return False


def interrupt_codex_turn(thread_key: str, codex_bin: str = "codex") -> bool:
    """Ask Codex App Server to interrupt an active turn, without ending its thread."""
    with _runtime_lock:
        runtime = _runtimes.get(codex_bin)
    if runtime is None:
        return False
    try:
        return bool(runtime.run(_interrupt_active_turn(runtime.client, thread_key), timeout=12))
    except Exception:
        return False


def steer_codex_turn(thread_key: str, prompt: str, codex_bin: str = "codex") -> bool:
    """Synchronous, non-creating steering entry point for Telegram replies."""
    with _runtime_lock:
        runtime = _runtimes.get(codex_bin)
    if runtime is None:
        return False
    try:
        return bool(runtime.run(_steer_active_turn(runtime.client, thread_key, prompt), timeout=12))
    except Exception:
        return False


class _AppServerRuntime:
    def __init__(self, codex_bin: str) -> None:
        self.loop = asyncio.new_event_loop()
        self.client = AppServerClient(codex_bin)
        self.thread = threading.Thread(target=self.loop.run_forever, name="codex-app-server", daemon=True)
        self.thread.start()

    def run(self, coroutine: Any, timeout: float | None = None) -> Any:
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout=timeout)
        except Exception:
            future.cancel()
            raise

    def close(self) -> None:
        try:
            self.run(self.client.close(), timeout=8)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=3)


_runtime_lock = threading.Lock()
_runtimes: dict[str, _AppServerRuntime] = {}


def _runtime_for(codex_bin: str) -> _AppServerRuntime:
    with _runtime_lock:
        runtime = _runtimes.get(codex_bin)
        if runtime is None:
            runtime = _AppServerRuntime(codex_bin)
            _runtimes[codex_bin] = runtime
        return runtime


def _close_runtimes() -> None:
    with _runtime_lock:
        runtimes = list(_runtimes.values())
        _runtimes.clear()
    for runtime in runtimes:
        runtime.close()


atexit.register(_close_runtimes)
