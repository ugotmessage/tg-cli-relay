from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tg_cli_relay.relay import effective_model_for_backend, resolve_cursor_model
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


if __name__ == "__main__":
    unittest.main()
