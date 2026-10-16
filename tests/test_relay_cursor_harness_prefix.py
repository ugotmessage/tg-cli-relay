from __future__ import annotations

import pytest

from tg_cli_relay.relay import apply_cursor_harness_prefix, apply_cursor_worker_prefix


def test_apply_cursor_harness_prefix_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TGR_CURSOR_HARNESS_PREFIX", raising=False)
    monkeypatch.delenv("TGR_CURSOR_PROMPT_PREFIX", raising=False)
    out = apply_cursor_harness_prefix("hello")
    assert out.startswith("Task Router decision: DIRECT")
    assert "Rules matched:" in out
    assert out.endswith("hello")


def test_apply_cursor_harness_prefix_custom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TGR_CURSOR_PROMPT_PREFIX", "CUSTOM\n")
    out = apply_cursor_harness_prefix("task")
    assert out.startswith("CUSTOM")
    assert out.endswith("task")


def test_apply_cursor_harness_prefix_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TGR_CURSOR_HARNESS_PREFIX", "0")
    assert apply_cursor_harness_prefix("plain") == "plain"


def test_apply_cursor_harness_prefix_disabled_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TGR_CURSOR_HARNESS_PREFIX", "false")
    assert apply_cursor_harness_prefix("plain") == "plain"


def test_cursor_worker_prefix_executes_without_recursive_delegation() -> None:
    out = apply_cursor_worker_prefix("請實作功能並跑測試")
    assert "Relay role: WORKER" in out
    assert "Execute the assigned scope directly" in out
    assert "do not dispatch, spawn, or delegate" in out
    assert out.endswith("請實作功能並跑測試")
