from __future__ import annotations

import os

import pytest

from tg_cli_relay.relay import DEFAULT_CURSOR_HARNESS_PREFIX, apply_cursor_harness_prefix


def test_apply_cursor_harness_prefix_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TGR_CURSOR_HARNESS_PREFIX", raising=False)
    monkeypatch.delenv("TGR_CURSOR_PROMPT_PREFIX", raising=False)
    out = apply_cursor_harness_prefix("hello")
    assert out.startswith(DEFAULT_CURSOR_HARNESS_PREFIX)
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
