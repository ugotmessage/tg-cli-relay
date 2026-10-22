from __future__ import annotations

from pathlib import Path

from tg_cli_relay.job_store import JobStore


def test_job_lifecycle(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "sessions.sqlite3")

    job = store.create(
        thread_key="private:123",
        backend="cursor",
        prompt="long task",
        reason="research/batch scope",
    )

    assert job.status == "queued"
    assert job.worker_thread_key == f"private:123::job:{job.id}"

    store.mark_running(job.id)
    running = store.get(job.id)
    assert running is not None
    assert running.status == "running"
    assert running.started_at is not None

    store.complete(job.id, "done")
    completed = store.get(job.id)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.result_preview == "done"
    assert completed.finished_at is not None


def test_jobs_are_scoped_by_thread(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "sessions.sqlite3")
    one = store.create(thread_key="private:1", backend="cursor", prompt="a")
    store.create(thread_key="private:2", backend="cursor", prompt="b")

    jobs = store.list_for_thread("private:1")

    assert [job.id for job in jobs] == [one.id]
