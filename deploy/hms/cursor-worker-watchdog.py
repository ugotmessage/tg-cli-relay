#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

HMS = Path("/Users/ht/.hermes")
QUEUE = HMS / "data/state/cursor-worker-watchdog/queue"
LOG_DIR = HMS / "logs/cursor-worker-watchdog"
RUNNER = HMS / "skills/autonomous-ai-agents/cursor-agent/scripts/run.py"
ENV_FILE = HMS / "secrets/tg-relay-cursor.env"
NOTIFY = HMS / "scripts/notify.py"
ALLOWED_WORKSPACES = (
    Path("/private/tmp"), HMS, Path("/Users/ht/.hermes-worktrees"),
    Path("/Users/ht/projects"), Path("/Volumes/home"),
)
ALLOWED_PROMPT_ROOTS = (HMS / "plans", HMS / "artifacts", Path("/Users/ht/Desktop"), Path("/Users/ht/Downloads"))
ALLOWED_ARTIFACT_ROOTS = (HMS / "artifacts", Path("/private/tmp"), Path("/Volumes/home"))


class TaskContractError(RuntimeError):
    """The queue task itself is invalid; rerunning the worker cannot repair it."""


class WorkerDoneValidationError(RuntimeError):
    """The worker delivered its artifact, but controller validation failed."""


class WorkerTimedOutError(RuntimeError):
    """The worker exceeded its configured timeout after retries."""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / "watchdog.log").open("a", encoding="utf-8") as handle:
        handle.write(f"{now()} {message}\n")


def notify(title: str, message: str) -> bool:
    result = subprocess.run(
        ["python3", str(NOTIFY), "--group", "notification", "--title", title, "--message", message],
        capture_output=True,
        text=True,
    )
    if not result.returncode:
        return True
    log(f"notification_primary_failed rc={result.returncode} stderr={result.stderr[-300:]!r}")
    fallback = subprocess.run(
        ["python3", str(NOTIFY), "--group", "main", "--title", title, "--message", message],
        capture_output=True,
        text=True,
    )
    if fallback.returncode:
        log(f"notification_fallback_failed rc={fallback.returncode} stderr={fallback.stderr[-300:]!r}")
        return False
    log("notification_fallback_ok group=main")
    return True


def under(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    return any(resolved == root.resolve() or root.resolve() in resolved.parents for root in roots)


def cursor_env() -> dict[str, str]:
    result = subprocess.run(
        ["/bin/zsh", "-c", f"set -a; source {ENV_FILE}; env -0"],
        capture_output=True,
        check=True,
    )
    env = os.environ.copy()
    for item in result.stdout.split(b"\0"):
        if b"=" in item:
            key, value = item.split(b"=", 1)
            env[key.decode(errors="ignore")] = value.decode(errors="ignore")
    env["PATH"] = "/Users/ht/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    return env


def validate(task: dict) -> tuple[Path, Path, Path]:
    workspace = Path(task["workspace"])
    prompt = Path(task["prompt_file"])
    artifact = Path(task["artifact"])
    if not under(workspace, ALLOWED_WORKSPACES):
        raise ValueError("workspace outside allowlist")
    if not under(prompt, ALLOWED_PROMPT_ROOTS):
        raise ValueError("prompt outside plans")
    if not under(artifact, ALLOWED_ARTIFACT_ROOTS):
        raise ValueError("artifact outside research root")
    if not workspace.is_dir() or not prompt.is_file():
        raise ValueError("workspace or prompt missing")
    return workspace, prompt, artifact


def preflight_validation_commands(task: dict, workspace: Path) -> None:
    """Reject deterministic queue mistakes before starting an expensive worker.

    In particular, pytest targets are controller-owned inputs and must already
    exist.  Generated artifact gates are intentionally checked only after the
    worker exits because they may legitimately be created by the task.
    """
    commands = task.get("validation_commands", [])
    if not isinstance(commands, list):
        raise TaskContractError("validation_commands must be a list")
    for index, command in enumerate(commands, 1):
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise TaskContractError(f"validation command {index} must be a non-empty argv list")
        if "pytest" not in command:
            continue
        pytest_index = command.index("pytest")
        for value in command[pytest_index + 1:]:
            if value.startswith("-") or not value.endswith(".py"):
                continue
            target = Path(value)
            target = target if target.is_absolute() else workspace / target
            if not target.is_file():
                raise TaskContractError(
                    f"validation command {index} pytest target missing before worker start: {target}"
                )


def report_references(task: dict, artifact: Path) -> str:
    preferred = [
        name for name in task.get("required_files", [])
        if name.endswith(".md") and ("report" in name.lower() or "recommend" in name.lower())
    ]
    existing = [str(artifact / name) for name in preferred if (artifact / name).is_file()]
    if not existing:
        existing = [str(path) for path in sorted(artifact.glob("*report*.md"))[:3]] if artifact.exists() else []
    return ", ".join(existing) if existing else "尚無可讀報告"


def evidence(task: dict, artifact: Path) -> tuple[bool, str]:
    missing = [name for name in task.get("required_files", []) if not (artifact / name).is_file()]
    receipts = {
        path.resolve()
        for pattern in task.get("receipt_patterns", ["*receipt*", "*validator*"])
        for path in artifact.rglob(pattern)
        if path.is_file()
    }
    minimum = int(task.get("receipt_min", 0))
    return not missing and len(receipts) >= minimum, f"missing={missing} receipts={len(receipts)}/{minimum}"


def newest_mtime(paths: list[Path]) -> float:
    newest = 0.0
    for root in paths:
        if not root.exists():
            continue
        if root.is_file():
            newest = max(newest, root.stat().st_mtime)
            continue
        for directory, _, files in os.walk(root):
            for name in files:
                try:
                    newest = max(newest, (Path(directory) / name).stat().st_mtime)
                except OSError:
                    pass
    return newest


def activity_paths(task: dict, workspace: Path, artifact: Path, run_log: Path) -> list[Path]:
    configured = [Path(value) for value in task.get("activity_paths", [])]
    if configured:
        return configured
    project_slug = re.sub(r"[^A-Za-z0-9]+", "-", str(workspace)).strip("-")
    return [
        run_log,
        artifact,
        Path("/Users/ht/.cursor/projects") / project_slug / "agent-transcripts",
    ]


def run_validation_commands(task: dict, workspace: Path, task_id: str) -> tuple[bool, str]:
    commands = task.get("validation_commands", [])
    if not isinstance(commands, list):
        return False, "validation_commands must be a list"
    for index, command in enumerate(commands, 1):
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            return False, f"validation command {index} must be a non-empty argv list"
        log_path = LOG_DIR / f"{task_id}-validation-{index}.log"
        with log_path.open("w", encoding="utf-8") as handle:
            result = subprocess.run(command, cwd=workspace, stdout=handle, stderr=subprocess.STDOUT)
        if result.returncode:
            return False, f"validation command {index} failed rc={result.returncode} log={log_path}"
    return True, f"validation_commands={len(commands)} passed"


def run_task(task_file: Path) -> None:
    task = json.loads(task_file.read_text(encoding="utf-8"))
    task_id = str(task["id"])
    workspace, prompt_file, artifact = validate(task)
    preflight_validation_commands(task, workspace)
    env = cursor_env()
    preflight = subprocess.run(["python3", str(RUNNER), "--preflight"], env=env, capture_output=True, text=True)
    if preflight.returncode:
        raise RuntimeError(f"preflight failed: {preflight.stderr[-300:]}")
    attempts = max(1, int(task.get("max_attempts", 3)))
    timeout = max(300, int(task.get("timeout_seconds", 7200)))
    prompt = prompt_file.read_text(encoding="utf-8")
    for attempt in range(1, attempts + 1):
        log(f"task={task_id} attempt={attempt} start")
        run_log = LOG_DIR / f"{task_id}-attempt-{attempt}.log"
        command = [
            "python3", str(RUNNER), "--print", "--model", "auto",
            "--workspace", str(workspace), "--timeout", str(timeout), "--prompt", prompt,
        ]
        heartbeat = max(60, int(task.get("heartbeat_seconds", 300)))
        stall_threshold = max(heartbeat, int(task.get("stall_threshold_seconds", 900)))
        watched = activity_paths(task, workspace, artifact, run_log)
        last_activity = time.time()
        last_heartbeat = time.time()
        stall_reported = False
        with run_log.open("w", encoding="utf-8") as handle:
            process = subprocess.Popen(command, cwd=workspace, env=env, stdout=handle, stderr=subprocess.STDOUT)
            while process.poll() is None:
                time.sleep(30)
                current = newest_mtime(watched)
                if current > last_activity:
                    last_activity = current
                    if stall_reported:
                        notify(
                            f"Cursor 任務恢復：{task_id}",
                            f"偵測到 transcript／程式／artifact 再次更新；attempt={attempt}，不需要使用者操作。",
                        )
                        stall_reported = False
                elapsed_idle = time.time() - last_activity
                if elapsed_idle >= stall_threshold and not stall_reported:
                    notify(
                        f"Cursor 任務疑似卡住：{task_id}",
                        f"attempt={attempt} 已 {int(elapsed_idle)} 秒沒有 transcript／Git／artifact 更新。watchdog 仍持續監看，若程序退出會自動重試。",
                    )
                    log(f"task={task_id} attempt={attempt} stalled idle_seconds={int(elapsed_idle)}")
                    stall_reported = True
                if time.time() - last_heartbeat >= heartbeat:
                    log(
                        f"task={task_id} attempt={attempt} heartbeat "
                        f"pid={process.pid} idle_seconds={int(elapsed_idle)} stalled={stall_reported}"
                    )
                    last_heartbeat = time.time()
            returncode = process.returncode
        evidence_ok, detail = evidence(task, artifact)
        validation_ok, validation_detail = run_validation_commands(task, workspace, task_id)
        detail = f"{detail}; {validation_detail}"
        log(f"task={task_id} attempt={attempt} exit={returncode} {detail}")
        if returncode == 0 and evidence_ok and validation_ok:
            notify(
                f"Cursor 任務完成：{task_id}",
                f"狀態=complete；產物與驗收命令均通過。{detail}。"
                f"報告：{report_references(task, artifact)}。待主 Agent 獨立驗收。",
            )
            return
        if returncode == 0 and evidence_ok and not validation_ok:
            raise WorkerDoneValidationError(
                f"狀態=worker_done/validation_failed；{validation_detail}；"
                f"產物已存在，不重跑昂貴 worker。報告：{report_references(task, artifact)}"
            )
        if attempt < attempts:
            notify(
                f"Cursor 任務重試：{task_id}",
                f"attempt {attempt} 未通過：exit={returncode}；{detail}。watchdog 將在 60 秒後自動重試，不需要使用者操作。",
            )
            time.sleep(60)
    if returncode == 124:
        raise WorkerTimedOutError(f"task timed out: {task_id}")
    raise RuntimeError(f"task exhausted retries: {task_id}")


CALLBACK_OUTBOX_BIN_DEFAULT = "/Users/ht/.hermes/scripts/research/hms_callback_outbox.py"
CALLBACK_DRAIN_TIMEOUT_DEFAULT = 900.0


def _callback_outbox_bin() -> str:
    return os.environ.get("HMS_CALLBACK_OUTBOX_BIN", CALLBACK_OUTBOX_BIN_DEFAULT)


def _callback_drain_timeout() -> float:
    raw = os.environ.get("HMS_CALLBACK_DRAIN_TIMEOUT", "").strip()
    if not raw:
        return CALLBACK_DRAIN_TIMEOUT_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return CALLBACK_DRAIN_TIMEOUT_DEFAULT
    if value < 60.0:
        return 60.0
    return value


def callback_enqueue(task_file: Path, controller_result: str, controller_detail: str) -> None:
    """Opt-in HMS→WEB1 callback. Transport failure must never raise."""
    try:
        subprocess.run(
            [
                "python3",
                _callback_outbox_bin(),
                "enqueue",
                "--task-json",
                str(task_file),
                "--controller-result",
                controller_result,
                "--controller-detail",
                str(controller_detail)[:2000],
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception as exc:
        log(f"callback_enqueue_failed task={task_file.name} error={type(exc).__name__}")


def callback_drain() -> None:
    """One bounded drain from the existing daemon loop; no model verifier."""
    try:
        subprocess.run(
            ["python3", _callback_outbox_bin(), "drain"],
            capture_output=True,
            text=True,
            timeout=_callback_drain_timeout(),
            check=False,
        )
    except Exception as exc:
        log(f"callback_drain_failed error={type(exc).__name__}")


def main() -> None:
    for state in ("pending", "running", "done", "failed"):
        (QUEUE / state).mkdir(parents=True, exist_ok=True)
    log("daemon_started")
    while True:
        pending = sorted((QUEUE / "pending").glob("*.json"))
        if not pending:
            callback_drain()
            time.sleep(10)
            continue
        source = pending[0]
        running = QUEUE / "running" / source.name
        terminal_path = None
        controller_result = None
        controller_detail = ""
        try:
            source.replace(running)
            run_task(running)
            running.replace(QUEUE / "done" / running.name)
            terminal_path = QUEUE / "done" / running.name
            controller_result = "complete"
            controller_detail = "status=complete"
        except WorkerDoneValidationError as exc:
            log(f"task={source.stem} validation_failed error={exc!r}")
            notify(
                f"Cursor 任務產物完成但驗收失敗：{source.stem}",
                f"{exc}。已停止自動重跑，需由主 Agent 修正驗收或程式；不需要使用者重新派工。",
            )
            if running.exists():
                running.replace(QUEUE / "failed" / running.name)
            terminal_path = QUEUE / "failed" / running.name
            controller_result = "worker_done_validation_failed"
            controller_detail = str(exc)
        except TaskContractError as exc:
            log(f"task={source.stem} task_contract_invalid error={exc!r}")
            notify(
                f"Cursor 任務設定錯誤：{source.stem}",
                f"狀態=blocked_before_worker；{exc}。昂貴 worker 未啟動，需由主 Agent修正 queue task。",
            )
            if running.exists():
                running.replace(QUEUE / "failed" / running.name)
            terminal_path = QUEUE / "failed" / running.name
            controller_result = "blocked_before_worker"
            controller_detail = str(exc)
        except WorkerTimedOutError as exc:
            log(f"task={source.stem} timed_out error={exc!r}")
            notify(f"Cursor 任務逾時：{source.stem}", f"共用 watchdog 已保留日誌：{exc}")
            if running.exists():
                running.replace(QUEUE / "failed" / running.name)
            terminal_path = QUEUE / "failed" / running.name
            controller_result = "timed_out"
            controller_detail = str(exc)
        except Exception as exc:
            log(f"task={source.stem} failed error={exc!r}")
            notify(f"Cursor 任務失敗：{source.stem}", f"共用 watchdog 已保留日誌：{exc}")
            if running.exists():
                running.replace(QUEUE / "failed" / running.name)
            terminal_path = QUEUE / "failed" / running.name
            controller_result = "failed"
            controller_detail = str(exc)
        if controller_result and terminal_path is not None and terminal_path.exists():
            callback_enqueue(terminal_path, controller_result, controller_detail)
        callback_drain()
        time.sleep(2)


if __name__ == "__main__":
    main()
