"""Small, capability-aware worker preference resolver.

Native means: let the selected primary provider use its own agent tools and
model catalog, rather than requiring a separate Cursor installation.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

AVAILABLE = ("cursor", "claude", "opencode")


@dataclass(frozen=True, slots=True)
class WorkerChoice:
    mode: str                 # "native" | "external" | "off"
    provider: str | None = None


def _installed(binary: str) -> bool:
    expanded = os.path.expanduser(binary)
    return bool(shutil.which(expanded) or (Path(expanded).is_file() and os.access(expanded, os.X_OK)))


def choose_codex_worker() -> WorkerChoice:
    """Resolve override > legacy override > auto-detect; never require Cursor."""
    raw = os.environ.get("TGR_WORKER_PROVIDER")
    if raw is None:
        legacy = os.environ.get("TGR_CODEX_DELEGATE_TO_CURSOR")
        if legacy is not None:
            raw = "cursor" if legacy.strip().lower() not in ("0", "false", "off", "no") else "native"
        else:
            raw = "auto"
    raw = raw.strip().lower()
    if raw in ("off", "none", "disabled"):
        return WorkerChoice("off")
    if raw in ("native", "codex"):
        return WorkerChoice("native")
    if raw == "auto":
        # Do not spawn a missing executable on every delegated request.
        if _installed(os.environ.get("TGR_CURSOR_AGENT_BIN", "agent")):
            return WorkerChoice("external", "cursor")
        return WorkerChoice("native")
    if raw in AVAILABLE:
        # An explicit external choice falls back safely if missing.
        binaries = {
            "cursor": ("TGR_CURSOR_AGENT_BIN", "agent"),
            "claude": ("TGR_CLAUDE_BIN", "claude"),
            "opencode": ("TGR_OPENCODE_BIN", "opencode"),
        }
        envvar, default = binaries[raw]
        if _installed(os.environ.get(envvar, default)):
            return WorkerChoice("external", raw)
        # Legacy forced Cursor was historically attempted; maintain compatible
        # mocks/tests. Real subprocess startup failures still fall back to Codex.
        if "TGR_WORKER_PROVIDER" not in os.environ and raw == "cursor":
            return WorkerChoice("external", "cursor")
        return WorkerChoice("native")
    raise ValueError("TGR_WORKER_PROVIDER must be auto, native, off, cursor, claude or opencode")
