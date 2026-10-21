from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

def make_task_title(prompt: str, max_len: int = 48) -> str:
    """Concise routing title only; never persist the full prompt."""
    text = " ".join((prompt or "").split())
    if not text:
        return "untitled"
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


def worker_key_for(parent_key: str, task_id: str) -> str:
    return f"{parent_key}:job:{task_id}"


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: str
    parent_key: str
    worker_key: str
    backend: str
    title: str
    status: str
    created_at: str
    updated_at: str


class TaskRegistry:
    """SQLite task registry and Telegram message→task bindings.

    Lives in the same per-bot session DB. Schema is additive: sessions and
    preferences tables are never rewritten.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @property
    def db_path(self) -> Path:
        return self._db_path

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
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
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    parent_key TEXT NOT NULL,
                    worker_key TEXT NOT NULL UNIQUE,
                    backend TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_messages (
                    parent_key TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    task_id TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (parent_key, message_id),
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_key, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_messages_task ON task_messages(task_id)"
            )

    def create_task(
        self,
        *,
        parent_key: str,
        backend: str,
        title: str,
        status: str = "queued",
    ) -> TaskRecord:
        safe_title = make_task_title(title)
        last_error: Exception | None = None
        for _ in range(8):
            task_id = uuid.uuid4().hex[:8]
            worker_key = worker_key_for(parent_key, task_id)
            try:
                with self._conn() as conn:
                    conn.execute(
                        """
                        INSERT INTO tasks (
                            task_id, parent_key, worker_key, backend, title, status,
                            created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                        """,
                        (task_id, parent_key, worker_key, backend, safe_title, status),
                    )
                record = self.get_task(task_id)
                if record is None:
                    raise RuntimeError("task insert succeeded but row is missing")
                return record
            except sqlite3.IntegrityError as exc:
                last_error = exc
                continue
        raise RuntimeError(f"unable to allocate unique task id: {last_error}")

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return _row_to_task(row) if row is not None else None

    def list_tasks(self, parent_key: str, *, limit: int = 10) -> list[TaskRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE parent_key = ?
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                (parent_key, limit),
            ).fetchall()
        return [_row_to_task(row) for row in rows]

    def update_status(self, task_id: str, status: str) -> bool:
        """Update one task by id. Never matches sibling tasks on the same parent."""
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = datetime('now')
                WHERE task_id = ?
                """,
                (status, task_id),
            )
            return cur.rowcount > 0

    def bind_message(
        self,
        *,
        parent_key: str,
        message_id: int,
        task_id: str,
        backend: str,
        role: str,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO task_messages (
                    parent_key, message_id, task_id, backend, role, created_at
                )
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(parent_key, message_id) DO NOTHING
                """,
                (parent_key, int(message_id), task_id, backend, role),
            )

    def get_task_for_message(self, parent_key: str, message_id: int) -> TaskRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT t.*
                FROM task_messages m
                JOIN tasks t ON t.task_id = m.task_id
                WHERE m.parent_key = ? AND m.message_id = ?
                """,
                (parent_key, int(message_id),),
            ).fetchone()
        return _row_to_task(row) if row is not None else None

    def interrupt_inflight(self) -> int:
        """Mark queued/running rows interrupted after process restart. Do not rerun."""
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE tasks
                SET status = 'interrupted', updated_at = datetime('now')
                WHERE status IN ('queued', 'starting', 'running')
                """
            )
            return cur.rowcount


def _row_to_task(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        task_id=str(row["task_id"]),
        parent_key=str(row["parent_key"]),
        worker_key=str(row["worker_key"]),
        backend=str(row["backend"]),
        title=str(row["title"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
