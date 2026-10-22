from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Mapping


class Route(StrEnum):
    DIRECT = "DIRECT"
    DELEGATE = "DELEGATE"


@dataclass(frozen=True, slots=True)
class RouterRule:
    id: str
    route: Route
    weight: int = 1
    description: str = ""
    keywords: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskRouterConfig:
    enabled: bool = True
    threshold: int = 3
    prompt_header: str = "Task Router"
    direct_prompt: str = (
        "Route DIRECT: handle the request in the current Cursor session. "
        "Do not spawn workers unless the user explicitly broadens scope."
    )
    delegate_prompt: str = (
        "Route DELEGATE: act only as Orchestrator. Use Job Dispatcher to split the work "
        "into isolated worker sessions for implementation/investigation/review as needed. "
        "Preserve worker session isolation; the main session coordinates, verifies, and reports. "
        "For long-running or batch work, launch a bounded background job, return a handle/status, "
        "and reply immediately after launch; never wait for completion, block the main relay session "
        "with foreground polling, or use long sleep loops."
    )
    common_prompt: str = (
        "Maintain existing DIRECT / DELEGATE behavior. Keep routing rule-based; do not invoke "
        "an LLM classifier. Reply in 繁體中文 unless the user asks otherwise."
    )
    rules: tuple[RouterRule, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class LoadedRouterConfig:
    config: TaskRouterConfig
    source: str
    fallback_used: bool
    message: str = ""


@dataclass(frozen=True, slots=True)
class MatchedRule:
    id: str
    route: Route
    weight: int
    description: str
    matched: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RouterDecision:
    route: Route
    delegate_score: int
    direct_score: int
    threshold: int
    matched_rules: tuple[MatchedRule, ...]
    enabled: bool = True

    @property
    def reason(self) -> str:
        if not self.enabled:
            return "router disabled"
        if not self.matched_rules:
            return "no rules matched"
        parts = [f"{m.id}:{m.weight}" for m in self.matched_rules]
        return ", ".join(parts)


DEFAULT_RULES: tuple[RouterRule, ...] = (
    RouterRule(
        id="explicit_worker_delegation",
        route=Route.DELEGATE,
        weight=10,
        description="An explicit request to dispatch a worker must override incidental status wording.",
        patterns=(
            r"(?:派|交給|讓).{0,8}(?:子\s*)?(?:worker|agent).{0,8}(?:做|跑|補|處理|執行)",
            r"\b(?:delegate|dispatch|spawn)\b.{0,16}\b(?:worker|agent)\b",
        ),
    ),
    RouterRule(
        id="quick_status_or_count",
        route=Route.DIRECT,
        weight=4,
        description="Small status/count/process questions stay in the current session.",
        keywords=("幾個", "有幾個", "目前", "現在", "status", "ps", "process", "relay 在跑"),
        patterns=(r"\b(how many|status|list)\b",),
    ),
    RouterRule(
        id="simple_question",
        route=Route.DIRECT,
        weight=3,
        description="Read-only questions and quick checks should be handled directly.",
        keywords=("可以嗎", "能不能", "查一下", "看一下", "是否", "有沒有", "版本", "路徑"),
        patterns=(r"^(what|which|where|is|are)\b",),
    ),
    RouterRule(
        id="multi_file_change",
        route=Route.DELEGATE,
        weight=4,
        description="Multi-file or broad refactor/build work should use workers.",
        keywords=("跨檔", "多檔", "三個檔案", "3+ 檔", "重構", "refactor", "架構", "升級"),
        patterns=(r"\b(3\+|three|multiple|many)\s+(files?|modules?|scopes?)\b",),
    ),
    RouterRule(
        id="coding_implementation",
        route=Route.DELEGATE,
        weight=4,
        description="Coding and implementation are execution work for a Cursor worker; the orchestrator reviews.",
        keywords=(
            "寫程式", "改程式", "修改程式", "實作", "開發功能", "新增功能", "修 bug", "修bug",
            "補測試", "寫測試", "coding", "implement", "implementation", "fix bug",
        ),
        patterns=(
            r"\b(?:add|build|create|develop|implement|patch|refactor)\b.{0,24}\b(?:code|feature|function|module|test)s?\b",
            r"\bfix(?:ing|ed)?\b.{0,16}\b(?:bug|code|test)s?\b",
        ),
    ),
    RouterRule(
        id="full_delivery_pipeline",
        route=Route.DELEGATE,
        weight=5,
        description="Implementation plus tests/review/PR is a dispatcher workflow.",
        keywords=("補 tests", "完整測試", "跑測試", "lint", "build", "review diff", "建立 PR", "開 PR", "pull request"),
        patterns=(r"\b(test|lint|build).*(pr|pull request)\b", r"\bpr\b.*\b(test|lint|build)\b"),
    ),
    RouterRule(
        id="batch_backfill_job",
        route=Route.DELEGATE,
        weight=4,
        description="Batch/backfill/long-running data jobs should run in isolated background workers.",
        keywords=(
            "批次", "大量", "分批", "補檔", "回補", "慢慢補", "背景執行", "長時間",
            "持續監控", "等待完成", "backfill", "in batches", "batch job", "background job",
            "duckdb", "finmind", "tagged universe",
        ),
        patterns=(
            r"\b(?:monitor|poll|wait)\b.*\b(?:finish|complete|progress)\b",
            r"\b(?:backfill|batch)\b.*\b(?:data|files?|rows?|universe|database)\b",
        ),
    ),
    RouterRule(
        id="high_risk_boundary",
        route=Route.DELEGATE,
        weight=4,
        description="DB/deploy/auth/external side effects need isolated investigation and verification.",
        keywords=(
            "DB", "database", "migration", "部署", "deploy", "登入", "login", "權限", "permission",
            "金流", "payment", "外部發送", "寄信", "交易", "restart", "systemd",
        ),
    ),
    RouterRule(
        id="debug_investigation",
        route=Route.DELEGATE,
        weight=3,
        description="Non-trivial debugging/investigation should be split from implementation.",
        keywords=("debug", "除錯", "排查", "調查", "root cause", "失敗", "錯誤", "troubleshoot"),
    ),
)


def default_task_router_config() -> TaskRouterConfig:
    return TaskRouterConfig(rules=DEFAULT_RULES)


def _default_config_candidates(env: Mapping[str, str]) -> tuple[Path, ...]:
    explicit = env.get("TGR_ROUTER_CONFIG", "").strip()
    if explicit:
        return (Path(explicit).expanduser(),)
    return (Path("router.toml"), Path("config") / "router.toml")


def load_task_router_config(env: Mapping[str, str] | None = None) -> LoadedRouterConfig:
    env_map = os.environ if env is None else env
    default = default_task_router_config()
    candidates = _default_config_candidates(env_map)
    for path in candidates:
        try:
            if not path.is_file():
                continue
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            return LoadedRouterConfig(
                config=_config_from_toml(data),
                source=str(path),
                fallback_used=False,
                message="loaded",
            )
        except Exception as exc:
            return LoadedRouterConfig(
                config=default,
                source=str(path),
                fallback_used=True,
                message=f"error loading router config: {exc}",
            )
    return LoadedRouterConfig(
        config=default,
        source=", ".join(str(p) for p in candidates),
        fallback_used=True,
        message="config missing; using built-in defaults",
    )


def _config_from_toml(data: Mapping[str, object]) -> TaskRouterConfig:
    router_raw = data.get("router", {})
    if not isinstance(router_raw, Mapping):
        raise ValueError("[router] must be a table")
    rules_raw = data.get("rules", [])
    if not isinstance(rules_raw, list):
        raise ValueError("[[rules]] must be an array of tables")
    rules = tuple(_rule_from_mapping(item) for item in rules_raw)
    if not rules:
        rules = DEFAULT_RULES
    defaults = TaskRouterConfig()
    return TaskRouterConfig(
        enabled=bool(router_raw.get("enabled", defaults.enabled)),
        threshold=int(router_raw.get("threshold", defaults.threshold)),
        prompt_header=str(router_raw.get("prompt_header", defaults.prompt_header)),
        direct_prompt=str(router_raw.get("direct_prompt", defaults.direct_prompt)),
        delegate_prompt=str(router_raw.get("delegate_prompt", defaults.delegate_prompt)),
        common_prompt=str(router_raw.get("common_prompt", defaults.common_prompt)),
        rules=rules,
    )


def _tuple_of_str(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    return tuple(str(item) for item in value if str(item).strip())


def _rule_from_mapping(raw: object) -> RouterRule:
    if not isinstance(raw, Mapping):
        raise ValueError("rule must be a table")
    route_raw = str(raw.get("route", "")).strip().upper()
    if route_raw not in (Route.DIRECT.value, Route.DELEGATE.value):
        raise ValueError(f"invalid rule route: {route_raw}")
    return RouterRule(
        id=str(raw.get("id", "unnamed")).strip() or "unnamed",
        route=Route(route_raw),
        weight=int(raw.get("weight", 1)),
        description=str(raw.get("description", "")),
        keywords=_tuple_of_str(raw.get("keywords"), "keywords"),
        patterns=_tuple_of_str(raw.get("patterns"), "patterns"),
    )


def route_text(text: str, config: TaskRouterConfig | None = None) -> RouterDecision:
    cfg = config or load_task_router_config().config
    if not cfg.enabled:
        return RouterDecision(Route.DIRECT, 0, 0, cfg.threshold, (), enabled=False)
    haystack = text.casefold()
    matched: list[MatchedRule] = []
    delegate_score = 0
    direct_score = 0
    for rule in cfg.rules:
        hits: list[str] = []
        for keyword in rule.keywords:
            if keyword.casefold() in haystack:
                hits.append(keyword)
        for pattern in rule.patterns:
            if re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE):
                hits.append(f"/{pattern}/")
        if not hits:
            continue
        matched_rule = MatchedRule(rule.id, rule.route, rule.weight, rule.description, tuple(hits))
        matched.append(matched_rule)
        if rule.route == Route.DELEGATE:
            delegate_score += rule.weight
        else:
            direct_score += rule.weight
    # Implementation intent should not lose to incidental status words on a tie.
    implementation_intent = any(
        m.id in ("coding_implementation", "full_delivery_pipeline", "explicit_worker_delegation")
        and m.route == Route.DELEGATE for m in matched
    )
    route = (
        Route.DELEGATE if delegate_score >= cfg.threshold
        and (delegate_score > direct_score or (implementation_intent and delegate_score == direct_score))
        else Route.DIRECT
    )
    return RouterDecision(route, delegate_score, direct_score, cfg.threshold, tuple(matched))


def render_router_prefix(text: str, loaded: LoadedRouterConfig | None = None) -> str:
    loaded_cfg = loaded or load_task_router_config()
    cfg = loaded_cfg.config
    decision = route_text(text, cfg)
    route_prompt = cfg.delegate_prompt if decision.route == Route.DELEGATE else cfg.direct_prompt
    matched = "; ".join(
        f"{m.id}({m.route.value.lower()} +{m.weight}: {', '.join(m.matched)})"
        for m in decision.matched_rules
    ) or "none"
    load_state = "fallback" if loaded_cfg.fallback_used else "external"
    lines = [
        f"{cfg.prompt_header} decision: {decision.route.value}",
        f"Scores: delegate={decision.delegate_score}, direct={decision.direct_score}, threshold={decision.threshold}",
        f"Rules matched: {matched}",
        f"Config: {load_state} ({loaded_cfg.source})",
        cfg.common_prompt,
        route_prompt,
    ]
    return "\n".join(line for line in lines if line).strip() + "\n\n"


def format_router_status(loaded: LoadedRouterConfig | None = None) -> str:
    loaded_cfg = loaded or load_task_router_config()
    cfg = loaded_cfg.config
    state = "fallback" if loaded_cfg.fallback_used else "external"
    lines = [
        "Task Router",
        f"狀態: {'enabled' if cfg.enabled else 'disabled'}",
        f"設定來源: {state} — {loaded_cfg.source}",
        f"訊息: {loaded_cfg.message}",
        f"threshold: {cfg.threshold}",
        "",
        "規則:",
    ]
    for rule in cfg.rules:
        terms = ", ".join(rule.keywords + rule.patterns) or "(no matchers)"
        desc = f" — {rule.description}" if rule.description else ""
        lines.append(f"- {rule.id}: {rule.route.value.lower()} +{rule.weight}{desc}")
        lines.append(f"  match: {terms}")
    lines.extend(["", "用法: /router", "用法: /router test <文字>"])
    return "\n".join(lines)


def format_router_test(text: str, loaded: LoadedRouterConfig | None = None) -> str:
    loaded_cfg = loaded or load_task_router_config()
    decision = route_text(text, loaded_cfg.config)
    lines = [
        f"Task Router decision: {decision.route.value}",
        f"delegate={decision.delegate_score}, direct={decision.direct_score}, threshold={decision.threshold}",
        f"reason: {decision.reason}",
    ]
    if decision.matched_rules:
        lines.append("matched rules:")
        for match in decision.matched_rules:
            lines.append(
                f"- {match.id}: {match.route.value.lower()} +{match.weight} ({', '.join(match.matched)})"
            )
    return "\n".join(lines)
