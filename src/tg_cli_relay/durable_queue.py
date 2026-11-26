"""Crash-safe intake ledger for Telegram messages handed to Relay.

queued: never started, eligible for replay after a clean restart.
running: began executing, result may be unknown after a crash => needs_review.
needs_review: NEVER replay automatically; operator explicitly releases backlog.
done/cancelled/reviewed: terminal audit rows, not replayed.

Telegram turns keep UNIQUE(thread_key, message_id, kind) except
background_continuation rows, which are keyed by the lossless event_id.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class QueueRecord:
    id: int
    thread_key: str
    backend: str
    prompt: str
    kind: str
    chat_id: int
    message_id: int
    topic_id: int | None
    task_id: str | None
    worker_key: str | None
    title: str
    status: str
    event_id: str | None = None
    resume_session_id: str | None = None


_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS relay_pending_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_key TEXT NOT NULL,
    backend TEXT NOT NULL,
    prompt TEXT NOT NULL,
    kind TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    topic_id INTEGER,
    task_id TEXT,
    worker_key TEXT,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    event_id TEXT,
    resume_session_id TEXT
)
"""


class DurableQueue:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as con:
            con.execute(_TABLE_SQL)
            self._migrate(con)
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_relay_queue
                ON relay_pending_turns(thread_key,status,id)
            """)
            con.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_relay_queue_telegram
                ON relay_pending_turns(thread_key, message_id, kind)
                WHERE kind != 'background_continuation'
            """)
            con.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_relay_queue_event
                ON relay_pending_turns(event_id)
                WHERE event_id IS NOT NULL
            """)

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return _Txn(conn)

    @staticmethod
    def _migrate(con: sqlite3.Connection) -> None:
        cols = {row[1] for row in con.execute("PRAGMA table_info(relay_pending_turns)")}
        if not cols:
            return
        if "event_id" in cols and "resume_session_id" in cols:
            return
        con.execute("""
            CREATE TABLE relay_pending_turns_next (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_key TEXT NOT NULL,
                backend TEXT NOT NULL,
                prompt TEXT NOT NULL,
                kind TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                topic_id INTEGER,
                task_id TEXT,
                worker_key TEXT,
                title TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                event_id TEXT,
                resume_session_id TEXT
            )
        """)
        con.execute("""
            INSERT INTO relay_pending_turns_next (
                id, thread_key, backend, prompt, kind, chat_id, message_id,
                topic_id, task_id, worker_key, title, status, created_at, updated_at,
                event_id, resume_session_id
            )
            SELECT id, thread_key, backend, prompt, kind, chat_id, message_id,
                topic_id, task_id, worker_key, title, status, created_at, updated_at,
                NULL, NULL
            FROM relay_pending_turns
        """)
        con.execute("DROP TABLE relay_pending_turns")
        con.execute("ALTER TABLE relay_pending_turns_next RENAME TO relay_pending_turns")

    def add(self, *, thread_key: str, backend: str, prompt: str, kind: str,
            chat_id: int, message_id: int, topic_id: int | None,
            task_id: str | None = None, worker_key: str | None = None,
            title: str = "", event_id: str | None = None,
            resume_session_id: str | None = None) -> int:
        with self._conn() as con:
            try:
                con.execute("""
                    INSERT INTO relay_pending_turns (
                        thread_key,backend,prompt,kind,chat_id,message_id,
                        topic_id,task_id,worker_key,title,status,
                        event_id,resume_session_id
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,'queued',?,?)
                """, (thread_key, backend, prompt, kind, chat_id, message_id,
                      topic_id, task_id, worker_key, title, event_id,
                      resume_session_id))
            except sqlite3.IntegrityError:
                pass
            if event_id:
                row = con.execute(
                    "SELECT id FROM relay_pending_turns WHERE event_id=?",
                    (event_id,),
                ).fetchone()
            else:
                row = con.execute("""
                    SELECT id FROM relay_pending_turns
                    WHERE thread_key=? AND message_id=? AND kind=?
                """, (thread_key, message_id, kind)).fetchone()
            assert row is not None
            return int(row["id"])

    def lookup(self, *, thread_key: str, message_id: int, kind: str) -> QueueRecord | None:
        with self._conn() as con:
            row = con.execute(
                """
                SELECT * FROM relay_pending_turns
                WHERE thread_key=? AND message_id=? AND kind=?
                """,
                (thread_key, message_id, kind),
            ).fetchone()
            return _record(row) if row is not None else None

    def lookup_event(self, event_id: str) -> QueueRecord | None:
        if not event_id:
            return None
        with self._conn() as con:
            row = con.execute(
                "SELECT * FROM relay_pending_turns WHERE event_id=?",
                (event_id,),
            ).fetchone()
            return _record(row) if row is not None else None

    def get(self, qid: int) -> QueueRecord | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT * FROM relay_pending_turns WHERE id=?",
                (qid,),
            ).fetchone()
            return _record(row) if row is not None else None

    def start(self, qid: int) -> bool:
        with self._conn() as con:
            cur = con.execute("""
                UPDATE relay_pending_turns
                SET status='running',updated_at=datetime('now')
                WHERE id=? AND status='queued'
            """, (qid,))
            return cur.rowcount == 1

    def finish(self, qid: int, *, status: str = "done") -> None:
        if status not in ("done", "cancelled", "needs_review"):
            raise ValueError(status)
        with self._conn() as con:
            con.execute("""
                UPDATE relay_pending_turns SET status=?,updated_at=datetime('now')
                WHERE id=?
            """, (status,qid))

    def mark_interrupted_uncertain(self) -> int:
        with self._conn() as con:
            cur = con.execute("""
                UPDATE relay_pending_turns
                SET status='needs_review',updated_at=datetime('now')
                WHERE status='running'
            """)
            return cur.rowcount

    def reviewed(self, thread_key: str) -> int:
        with self._conn() as con:
            cur = con.execute("""
                UPDATE relay_pending_turns
                SET status='reviewed',updated_at=datetime('now')
                WHERE thread_key=? AND status='needs_review'
            """, (thread_key,))
            return cur.rowcount

    def prune_terminal(self, *, retention_days: int = 30) -> int:
        """Bound persisted message content without deleting waiting/review rows."""
        if retention_days < 1:
            raise ValueError("retention_days must be positive")
        with self._conn() as con:
            cur = con.execute("""
                DELETE FROM relay_pending_turns
                WHERE status IN ('done','cancelled','reviewed')
                  AND updated_at < datetime('now', ?)
            """, (f"-{retention_days} days",))
            return cur.rowcount

    def load_queued(self) -> list[QueueRecord]:
        with self._conn() as con:
            rows = con.execute("""
                SELECT * FROM relay_pending_turns
                WHERE status='queued'
                ORDER BY thread_key,
                  CASE WHEN kind='timeout_recovery' THEN 0 ELSE 1 END, id
            """).fetchall()
            return [_record(row) for row in rows]

    def open_items(self, thread_key: str) -> list[QueueRecord]:
        with self._conn() as con:
            rows = con.execute("""
                SELECT * FROM relay_pending_turns
                WHERE thread_key=? AND status IN ('queued','running','needs_review')
                ORDER BY CASE WHEN status='needs_review' THEN 0 ELSE 1 END,id
            """, (thread_key,)).fetchall()
            return [_record(row) for row in rows]

    def review_threads(self) -> set[str]:
        """Threads whose *main* execution is uncertain.

        Background continuations that crashed in-flight stay needs_review so
        they are never auto-replayed, but they must not pause ordinary chat.
        """
        with self._conn() as con:
            return {str(row[0]) for row in con.execute("""
                SELECT DISTINCT thread_key FROM relay_pending_turns
                WHERE status='needs_review'
                  AND kind != 'background_continuation'
            """)}


class _Txn:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        return self.conn

    def __exit__(self, *exc) -> None:
        try:
            if exc[0] is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()


def _record(row: sqlite3.Row) -> QueueRecord:
    keys = row.keys()
    return QueueRecord(
        id=int(row["id"]), thread_key=str(row["thread_key"]),
        backend=str(row["backend"]), prompt=str(row["prompt"]),
        kind=str(row["kind"]), chat_id=int(row["chat_id"]),
        message_id=int(row["message_id"]),
        topic_id=int(row["topic_id"]) if row["topic_id"] is not None else None,
        task_id=str(row["task_id"]) if row["task_id"] is not None else None,
        worker_key=str(row["worker_key"]) if row["worker_key"] is not None else None,
        title=str(row["title"]), status=str(row["status"]),
        event_id=(
            str(row["event_id"]) if "event_id" in keys and row["event_id"] is not None else None
        ),
        resume_session_id=(
            str(row["resume_session_id"])
            if "resume_session_id" in keys and row["resume_session_id"] is not None
            else None
        ),
    )
