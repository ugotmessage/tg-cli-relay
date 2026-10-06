from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Literal

ExecutionMode = Literal["direct", "delegate"]


@dataclass(frozen=True, slots=True)
class RouteDecision:
    mode: ExecutionMode
    prompt: str
    score: int
    reasons: tuple[str, ...]


_DELEGATE_PREFIXES = ("!job ", "!delegate ", "[job] ", "[delegate] ")
_DIRECT_PREFIXES = ("!direct ", "[direct] ")

_RESEARCH_RE = re.compile(
    r"(研究|深入分析|回測|掃描|盤點|分析.+(?:檔|個|筆|份)|比較.+(?:檔|個|份|月|週)|"
    r"research|investigate|backtest|batch|all files|entire repo|whole repo)",
    re.IGNORECASE,
)
_IMPLEMENT_RE = re.compile(
    r"(修改程式|改程式|實作|重構|修好|修復|開\s*PR|建立\s*PR|跑測試|測試全部|部署|"
    r"implement|refactor|fix.+tests?|run tests?|open.+PR|create.+PR|deploy)",
    re.IGNORECASE,
)
_BROAD_SCOPE_RE = re.compile(
    r"(批次|逐一|全部|所有|整個|大量|多個|batch|all files|entire repo|whole repo)",
    re.IGNORECASE,
)
_MULTI_STEP_RE = re.compile(
    r"(然後|接著|再來|最後|並且|同時|之後再|and then|after that|finally)",
    re.IGNORECASE,
)
_TIME_RANGE_RE = re.compile(
    r"(最近|過去|近).{0,8}(?:天|週|周|月|季|年)|"
    r"(?:last|past)\s+\d+\s+(?:days?|weeks?|months?|years?)",
    re.IGNORECASE,
)
_MANY_ITEMS_RE = re.compile(
    r"(?<!\d)\d{2,}\s*(?:檔|個|筆|份|stocks?|files?|items?)(?![A-Za-z])",
    re.IGNORECASE,
)


def _threshold() -> int:
    raw = os.environ.get("TGR_DELEGATE_SCORE", "3").strip() or "3"
    try:
        return max(1, int(raw))
    except ValueError:
        return 3


def route_prompt(prompt: str) -> RouteDecision:
    """Classify a turn before invoking a provider.

    This router is deliberately deterministic and cheap.  It can be overridden
    per-message with !job / !delegate or !direct, and globally through
    TGR_ROUTER_MODE=heuristic|always_delegate|always_direct.
    """

    text = (prompt or "").strip()
    lowered = text.lower()

    for prefix in _DELEGATE_PREFIXES:
        if lowered.startswith(prefix):
            return RouteDecision(
                mode="delegate",
                prompt=text[len(prefix) :].lstrip(),
                score=999,
                reasons=("explicit delegate override",),
            )
    for prefix in _DIRECT_PREFIXES:
        if lowered.startswith(prefix):
            return RouteDecision(
                mode="direct",
                prompt=text[len(prefix) :].lstrip(),
                score=0,
                reasons=("explicit direct override",),
            )

    mode = os.environ.get("TGR_ROUTER_MODE", "heuristic").strip().lower()
    if mode == "always_delegate":
        return RouteDecision("delegate", text, 999, ("TGR_ROUTER_MODE=always_delegate",))
    if mode == "always_direct":
        return RouteDecision("direct", text, 0, ("TGR_ROUTER_MODE=always_direct",))

    score = 0
    reasons: list[str] = []

    if len(text) >= 1600:
        score += 2
        reasons.append("large prompt")
    elif len(text) >= 700:
        score += 1
        reasons.append("medium-large prompt")

    if _RESEARCH_RE.search(text):
        score += 2
        reasons.append("research/batch scope")

    if _IMPLEMENT_RE.search(text):
        score += 3
        reasons.append("implementation/test scope")

    if _BROAD_SCOPE_RE.search(text):
        score += 1
        reasons.append("broad scope")

    if _MULTI_STEP_RE.search(text):
        score += 1
        reasons.append("multi-step wording")

    if _TIME_RANGE_RE.search(text):
        score += 1
        reasons.append("time-range analysis")

    if _MANY_ITEMS_RE.search(text):
        score += 2
        reasons.append("many-item scope")

    decision: ExecutionMode = "delegate" if score >= _threshold() else "direct"
    return RouteDecision(decision, text, score, tuple(reasons))
