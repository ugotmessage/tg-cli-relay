from __future__ import annotations

from tg_cli_relay.task_router import route_prompt


def test_simple_question_stays_direct(monkeypatch):
    monkeypatch.delenv("TGR_ROUTER_MODE", raising=False)
    monkeypatch.delenv("TGR_DELEGATE_SCORE", raising=False)

    decision = route_prompt("這個錯誤訊息是什麼意思？")

    assert decision.mode == "direct"
    assert decision.prompt == "這個錯誤訊息是什麼意思？"


def test_multi_step_research_is_delegated(monkeypatch):
    monkeypatch.delenv("TGR_ROUTER_MODE", raising=False)
    monkeypatch.delenv("TGR_DELEGATE_SCORE", raising=False)

    decision = route_prompt("幫我研究最近三個月 150 檔股票，逐一分析分點，然後跑回測")

    assert decision.mode == "delegate"
    assert decision.score >= 3


def test_explicit_job_override_strips_prefix(monkeypatch):
    monkeypatch.setenv("TGR_ROUTER_MODE", "always_direct")

    decision = route_prompt("!job 幫我檢查整個 repo")

    assert decision.mode == "delegate"
    assert decision.prompt == "幫我檢查整個 repo"


def test_explicit_direct_override_wins(monkeypatch):
    monkeypatch.setenv("TGR_ROUTER_MODE", "always_delegate")

    decision = route_prompt("!direct 幫我研究這個名詞")

    assert decision.mode == "direct"
    assert decision.prompt == "幫我研究這個名詞"
