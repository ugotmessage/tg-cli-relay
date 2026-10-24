"""Interrupt tracked CLI turns without changing agent delegation policy.

Only Relay-initiated turns carry a thread key. Interrupts target that key,
not arbitrary machine processes. POSIX process groups also stop CLI children.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
from contextlib import contextmanager
from collections.abc import Iterator

_lock = threading.Lock()
_active: dict[str, subprocess.Popen[str]] = {}
_requested: set[str] = set()


def interrupt_requested(thread_key: str) -> bool:
    with _lock:
        return thread_key in _requested


def _terminate(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except (OSError, ProcessLookupError):
        return

    # A stubborn subprocess must not hold the FIFO forever.
    def hard_stop() -> None:
        if proc.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (OSError, ProcessLookupError):
            pass

    timer = threading.Timer(3.0, hard_stop)
    timer.daemon = True
    timer.start()


def request_interrupt(thread_key: str) -> bool:
    """Latch stop for a starting CLI turn; signal it if already running.

    The caller MUST clear the latch after that turn releases its queue slot.
    Returns whether a tracked subprocess was present (not kill confirmation).
    """
    with _lock:
        _requested.add(thread_key)
        proc = _active.get(thread_key)
    if proc is not None:
        _terminate(proc)
    return proc is not None


def clear_interrupt(thread_key: str) -> None:
    with _lock:
        _requested.discard(thread_key)


@contextmanager
def tracked_process(thread_key: str | None, proc: subprocess.Popen[str]) -> Iterator[None]:
    if thread_key is not None:
        with _lock:
            if thread_key in _active:
                raise RuntimeError("Concurrent CLI processes for one Relay session")
            _active[thread_key] = proc
            should_stop = thread_key in _requested
        if should_stop:
            _terminate(proc)
    try:
        yield
    finally:
        if thread_key is not None:
            with _lock:
                if _active.get(thread_key) is proc:
                    _active.pop(thread_key, None)


def interruptible_run(
    cmd: list[str], *, thread_key: str, timeout: float | None = None,
    input: str | None = None, cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Popen equivalent of subprocess.run with externally controllable lifetime."""
    opts: dict = {
        "stdin": subprocess.PIPE if input is not None else subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "cwd": cwd,
        "env": env,
    }
    if os.name == "posix":
        opts["start_new_session"] = True
    elif os.name == "nt":
        opts["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(cmd, **opts)
    with tracked_process(thread_key, proc):
        try:
            stdout, stderr = proc.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate(proc)
            stdout, stderr = proc.communicate()
            raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
