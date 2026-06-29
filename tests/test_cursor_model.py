from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tg_cli_relay.relay import resolve_cursor_model
from tg_cli_relay.session_store import SessionStore
from tg_cli_relay.telegram_bot import _get_model as bot_get_model


class CursorModelResolutionTest(unittest.TestCase):
    def _store(self) -> SessionStore:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return SessionStore(Path(tmp.name) / "sessions.sqlite3")

    def test_no_pref_defaults_to_auto(self) -> None:
        store = self._store()
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(resolve_cursor_model(store, "thread-1"), "auto")

    def test_generic_model_pref_is_ignored_for_cursor(self) -> None:
        store = self._store()
        store.set_pref("thread-1", "model", "gemini-pro")
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(resolve_cursor_model(store, "thread-1"), "auto")

    def test_cursor_specific_pref_wins(self) -> None:
        store = self._store()
        store.set_pref("thread-1", "model", "gemini-pro")
        store.set_pref("thread-1", "model:cursor", "composer-2-fast")
        with patch.dict("os.environ", {"TGR_CURSOR_MODEL": "auto"}, clear=True):
            self.assertEqual(resolve_cursor_model(store, "thread-1"), "composer-2-fast")

    def test_cursor_env_wins_when_no_pref(self) -> None:
        store = self._store()
        with patch.dict("os.environ", {"TGR_CURSOR_MODEL": "gpt-5.5-medium"}, clear=True):
            self.assertEqual(resolve_cursor_model(store, "thread-1"), "gpt-5.5-medium")

    def test_status_helper_shows_cursor_default_auto(self) -> None:
        store = self._store()
        store.set_pref("thread-1", "model", "gemini-pro")
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(bot_get_model(store, "thread-1", "cursor"), "auto")

    def test_non_cursor_backends_ignore_generic_model_pref(self) -> None:
        store = self._store()
        store.set_pref("thread-1", "model", "gemini-pro")
        with patch.dict("os.environ", {"TGR_DEFAULT_MODEL_CLAUDE": "claude-opus"}, clear=True):
            self.assertIsNone(bot_get_model(store, "thread-1", "claude"))
        store.set_pref("thread-1", "model:claude", "claude-sonnet")
        self.assertEqual(bot_get_model(store, "thread-1", "claude"), "claude-sonnet")


if __name__ == "__main__":
    unittest.main()
