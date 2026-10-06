from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

from tg_cli_relay.session_store import Backend

JobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: int
    thread_key: str
    backend: Backend
    worker_thread_key: str
    prompt: str
    status: JobStatus
    reason: str | None
    result_preview: str | None
    error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None


class JobStore:
    """Persistent delegated-job state backed by the relay SQLite database."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_key TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    worker_thread_key TEXT NOT NULL DEFAULT '',
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    result_preview TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    started_at TEXT,
                    finished_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_thread_created "
                "ON jobs(thread_key, id DESC)"
            )

    def create(
        self,
        *,
        thread_key: str,
        backend: Backend,
        prompt: str,
        reason: str | None = None,
    ) -> JobRecord:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO jobs (thread_key, backend, prompt, status, reason)
                VALUES (?, ?, ?, 'queued', ?)
                """,
                (thread_key, backend, prompt, reason),
            )
            job_id = int(cur.lastrowid)
            worker_key = f"{thread_key}::job:{job_id}"
            conn.execute(
                "UPDATE jobs SET worker_thread_key = ? WHERE id = ?",
                (worker_key, job_id),
            )
        record = self.get(job_id)
        assert record is not None
        return record

    def mark_running(self, job_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'running', started_at = datetime('now'), error = NULL
                WHERE id = ?
                """,
                (job_id,),
            )

    def complete(self, job_id: int, result: str) -> None:
        preview = (result or "")[:4000]
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'completed', result_preview = ?, error = NULL,
                    finished_at = datetime('now')
                WHERE id = ?
                """,
                (preview, job_id),
            )

    def fail(self, job_id: int, error: str, result: str = "") -> None:
        preview = (result or "")[:4000]
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'failed', result_preview = ?, error = ?,
                    finished_at = datetime('now')
                WHERE id = ?
                """,
                (preview, (error or "")[:4000], job_id),
            )

    def cancel(self, job_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'cancelled', finished_at = datetime('now')
                WHERE id = ? AND status IN ('queued', 'running')
                """,
                (job_id,),
            )

    def get(self, job_id: int) -> JobRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def list_for_thread(self, thread_key: str, *, limit: int = 10) -> list[JobRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE thread_key = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (thread_key, max(1, limit)),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=int(row["id"]),
            thread_key=str(row["thread_key"]),
            backend=str(row["backend"]),  # type: ignore[arg-type]
            worker_thread_key=str(row["worker_thread_key"]),
            prompt=str(row["prompt"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            reason=str(row["reason"]) if row["reason"] is not None else None,
            result_preview=(
                str(row["result_preview"]) if row["result_preview"] is not None else None
            ),
            error=str(row["error"]) if row["error"] is not None else None,
            created_at=str(row["created_at"]),
            started_at=str(row["started_at"]) if row["started_at"] is not None else None,
            finished_at=str(row["finished_at"]) if row["finished_at"] is not None else None,
        )
