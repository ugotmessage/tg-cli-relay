from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal

Backend = Literal["cursor", "codex", "claude"]


class SessionStore:
    """thread_key + backend → 後端 session id（UUID 或 Codex session id）。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    thread_key TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    workspace TEXT,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (thread_key, backend)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS preferences (
                    thread_key TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (thread_key, key)
                )
                """
            )

    def get(self, thread_key: str, backend: Backend) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT session_id FROM sessions WHERE thread_key = ? AND backend = ?",
                (thread_key, backend),
            ).fetchone()
            return str(row[0]) if row else None

    def upsert(
        self,
        thread_key: str,
        backend: Backend,
        session_id: str,
        *,
        workspace: str | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO sessions (thread_key, backend, session_id, workspace, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(thread_key, backend) DO UPDATE SET
                    session_id = excluded.session_id,
                    workspace = COALESCE(excluded.workspace, sessions.workspace),
                    updated_at = excluded.updated_at
                """,
                (thread_key, backend, session_id, workspace),
            )

    def delete(self, thread_key: str, backend: Backend) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM sessions WHERE thread_key = ? AND backend = ?",
                (thread_key, backend),
            )

    def get_pref(self, thread_key: str, key: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM preferences WHERE thread_key = ? AND key = ?",
                (thread_key, key),
            ).fetchone()
            return str(row[0]) if row else None

    def set_pref(self, thread_key: str, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO preferences (thread_key, key, value, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(thread_key, key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (thread_key, key, value),
            )

    def delete_pref(self, thread_key: str, key: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM preferences WHERE thread_key = ? AND key = ?",
                (thread_key, key),
            )
