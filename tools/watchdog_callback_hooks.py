"""Enqueue-after-terminal helpers conceptually used by the Hermes watchdog patch.

Do not import live ``cursor-worker-watchdog.py``. Tests and the artifact patch
share this mapping: transport failure never changes the terminal classification.
"""

from __future__ import annotations

from typing import Callable

ControllerEnqueue = Callable[[str, str, str], None]
ControllerDrain = Callable[[], None]


def map_controller_result(kind: str, detail: str = "") -> tuple[str, str]:
    if kind == "success":
        return "complete", detail or "status=complete"
    if kind == "validation":
        return "worker_done_validation_failed", detail
    if kind == "contract":
        return "blocked_before_worker", detail
    if kind in {"timeout", "timed_out"}:
        return "timed_out", detail or "status=timed_out"
    return "failed", detail


def after_terminal_callback(
    *,
    classified: str,
    terminal_path: str,
    detail: str,
    enqueue: ControllerEnqueue,
    drain: ControllerDrain,
) -> str:
    """Call enqueue+drain only after the watcher already classified the task.

    Any exception from transport is swallowed. The returned classification is
    always the input ``classified`` value.
    """
    controller_result, controller_detail = map_controller_result(classified, detail)
    try:
        enqueue(terminal_path, controller_result, controller_detail)
        drain()
    except Exception:
        return classified
    return classified
