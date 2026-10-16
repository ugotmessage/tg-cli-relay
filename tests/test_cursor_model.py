from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tg_cli_relay.providers.base import RunResult
from tg_cli_relay.relay import effective_model_for_backend, relay_turn, resolve_cursor_model
from tg_cli_relay.session_store import SessionStore


class ResolveCursorModelTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = SessionStore(Path(self._tmpdir.name) / "sessions.sqlite3")
        self.key = "tg:private:1"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_defaults_to_auto_without_prefs(self) -> None:
        self.assertEqual(resolve_cursor_model(self.store, self.key), "auto")

    def test_ignores_legacy_generic_model_pref(self) -> None:
        self.store.set_pref(self.key, "model", "gemini-3.1-pro")
        self.assertEqual(resolve_cursor_model(self.store, self.key), "auto")

    def test_uses_cursor_specific_pref(self) -> None:
        self.store.set_pref(self.key, "model:cursor", "gpt-5.3-codex")
        self.assertEqual(resolve_cursor_model(self.store, self.key), "gpt-5.3-codex")

    def test_env_override_when_no_thread_pref(self) -> None:
        with patch.dict(os.environ, {"TGR_CURSOR_MODEL": "composer-2"}):
            self.assertEqual(resolve_cursor_model(self.store, self.key), "composer-2")

    def test_thread_pref_beats_env(self) -> None:
        self.store.set_pref(self.key, "model:cursor", "auto")
        with patch.dict(os.environ, {"TGR_CURSOR_MODEL": "composer-2"}):
            self.assertEqual(resolve_cursor_model(self.store, self.key), "auto")

    def test_effective_model_for_cursor_shows_auto(self) -> None:
        self.assertEqual(
            effective_model_for_backend(self.store, self.key, "cursor"),
            "auto",
        )


class CursorAgentModelFlagTest(unittest.TestCase):
    def test_run_turn_always_passes_model_flag(self) -> None:
        from tg_cli_relay.providers.cursor_agent import CursorAgentProvider

        with patch("tg_cli_relay.providers.cursor_agent.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "ok"
            run.return_value.stderr = ""
            prov = CursorAgentProvider(model="auto")
            prov.run_turn(workspace="/tmp/ws", session_id="chat-1", prompt="hi")
            cmd = run.call_args.args[0]
            self.assertIn("--model", cmd)
            self.assertEqual(cmd[cmd.index("--model") + 1], "auto")

    def test_delegate_task_uses_long_cursor_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            with patch("tg_cli_relay.relay.CursorAgentProvider") as provider_cls, patch.dict(
                os.environ,
                {"TGR_ROUTER_CONFIG": str(Path(tmp) / "missing-router.toml")},
                clear=False,
            ):
                provider_cls.return_value.create_session.return_value = "cursor-1"
                provider_cls.return_value.run_turn.return_value = RunResult(
                    stdout="done", stderr="", returncode=0
                )
                relay_turn(
                    thread_key="private:1",
                    backend="cursor",
                    prompt="請實作一個新功能",
                    store=store,
                    workspace=tmp,
                )
            self.assertEqual(provider_cls.call_args.kwargs["timeout_seconds"], 3600)

    def test_direct_task_keeps_short_cursor_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.sqlite3")
            with patch("tg_cli_relay.relay.CursorAgentProvider") as provider_cls, patch.dict(
                os.environ,
                {"TGR_ROUTER_CONFIG": str(Path(tmp) / "missing-router.toml")},
                clear=False,
            ):
                provider_cls.return_value.create_session.return_value = "cursor-2"
                provider_cls.return_value.run_turn.return_value = RunResult(
                    stdout="one", stderr="", returncode=0
                )
                relay_turn(
                    thread_key="private:2",
                    backend="cursor",
                    prompt="目前版本是什麼？",
                    store=store,
                    workspace=tmp,
                )
            self.assertEqual(provider_cls.call_args.kwargs["timeout_seconds"], 600)


if __name__ == "__main__":
    unittest.main()
