"""HMS standalone outbox: probe, fresh HMS verifier, durable final-result."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from tg_cli_relay.background_callback import (
    CONTINUATION_KIND,
    KIND_FINAL,
    KIND_PROBE,
    bot_fingerprint_from_env,
    frozen_codex_session_id,
    receive_event_bytes,
    register_from_relay_context,
    scan_once,
    sign_event as relay_sign,
)
from tg_cli_relay.durable_queue import DurableQueue
from test_task_isolation import FakeMessage
from test_background_callback import SENTINEL_SECRET, _callback_env

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import hms_callback_outbox as outbox  # noqa: E402


TERMINAL_RESULTS = (
    "complete",
    "worker_done_validation_failed",
    "blocked_before_worker",
    "failed",
    "timed_out",
)


def _task(tmp_path: Path, *, enabled: bool = True, task_id: str = "task-outbox-1") -> Path:
    handoff = tmp_path / "handoff.md"
    handoff.write_text("worker handoff", encoding="utf-8")
    artifact = tmp_path / "artifact"
    artifact.mkdir(exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    payload = {
        "id": task_id,
        "workspace": str(workspace),
        "artifact": str(artifact),
        "callback": {
            "enabled": enabled,
            "handoff_path": str(handoff),
            "artifact": str(artifact),
            "report_path": str(tmp_path / "report.md"),
            "validation_path": str(tmp_path / "validation.log"),
            "workspace": str(workspace),
        },
    }
    (tmp_path / "report.md").write_text("report", encoding="utf-8")
    (tmp_path / "validation.log").write_text("ok", encoding="utf-8")
    path = tmp_path / "task.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _outbox_env(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "outbox"
    secret = tmp_path / "hms.secret"
    secret.write_bytes(SENTINEL_SECRET)
    monkeypatch.setenv("HMS_CALLBACK_OUTBOX", str(root))
    monkeypatch.setenv("HMS_CALLBACK_SECRET_FILE", str(secret))
    monkeypatch.setenv("HMS_CALLBACK_SSH_CONFIG", str(tmp_path / "ssh-config"))
    monkeypatch.setenv("HMS_CALLBACK_SSH_ALIAS", "snote-internal-ssh")
    monkeypatch.setenv("HMS_CALLBACK_SSH_REMOTE_CMD", "/usr/local/sbin/tg-cli-relay-callback-receive")
    monkeypatch.setenv("HMS_CALLBACK_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.delenv("HMS_CALLBACK_LOCAL_RECEIVER_ARGV", raising=False)
    monkeypatch.delenv("HMS_CALLBACK_LOCAL_RECEIVER_ROUTES", raising=False)
    (tmp_path / "ssh-config").write_text("Host snote-internal-ssh\n", encoding="utf-8")
    monkeypatch.setattr(outbox, "utcnow_iso", lambda: "2026-10-28T03:54:00Z")
    return root


def _register(tmp_path: Path, task_id: str = "task-outbox-1") -> None:
    msg = FakeMessage(text="hi", chat_id=7, message_id=1)
    msg.from_user = SimpleNamespace(id=99)
    register_from_relay_context(
        task_id=task_id,
        handoff_path=str(tmp_path / "handoff.md"),
        msg=msg,
        thread_key="private:7",
    )


def test_enqueue_disabled_is_noop(monkeypatch, tmp_path):
    _outbox_env(monkeypatch, tmp_path)
    path = _task(tmp_path, enabled=False)
    assert outbox.main(["enqueue", "--task-json", str(path), "--controller-result", "complete"]) == 0
    assert list((tmp_path / "outbox" / "pending").glob("*.json")) == []


@pytest.mark.parametrize("controller_result", TERMINAL_RESULTS)
def test_each_terminal_skips_verifier_and_sends_passthrough_final(monkeypatch, tmp_path, controller_result):
    _callback_env(monkeypatch, tmp_path)
    root = _outbox_env(monkeypatch, tmp_path)
    _register(tmp_path)
    path = _task(tmp_path)
    captured: dict = {"kinds": [], "verifier": 0, "argv": None, "cwd": None, "prompt": None}

    def runner(argv, event_bytes, timeout):
        event = json.loads(event_bytes.decode("utf-8"))
        captured["kinds"].append(event["kind"])
        assert event["hmac"] == relay_sign(event, SENTINEL_SECRET)
        assert SENTINEL_SECRET not in event_bytes
        result = receive_event_bytes(event_bytes)
        return CompletedProcess(argv, result.code, (result.reason + "\n").encode(), b"")

    def verifier(argv, prompt, workspace, timeout):
        captured["verifier"] += 1
        captured["argv"] = argv
        captured["cwd"] = workspace
        captured["prompt"] = prompt
        captured["timeout"] = timeout
        assert workspace == str(tmp_path / "workspace")
        assert "resume" not in argv
        assert "exec" in argv
        assert workspace in argv
        assert SENTINEL_SECRET.decode() not in " ".join(argv)
        assert SENTINEL_SECRET.decode() not in prompt
        assert controller_result in prompt
        assert "Traditional Chinese" in prompt or "獨立" in prompt
        return CompletedProcess(argv, 0, "獨立驗收：通過\n".encode(), b"")

    outbox.ssh_runner = runner
    outbox.verifier_runner = verifier
    try:
        assert outbox.main(
            [
                "enqueue",
                "--task-json",
                str(path),
                "--controller-result",
                controller_result,
                "--controller-detail",
                "ok",
            ]
        ) == 0
        assert outbox.main(["drain"]) == 0
    finally:
        outbox.ssh_runner = None
        outbox.verifier_runner = None
    assert captured["kinds"] == [KIND_PROBE, KIND_FINAL]
    assert captured["verifier"] == 0
    sent = json.loads(next((root / "sent").glob("*.json")).read_text(encoding="utf-8"))
    assert sent["verifier"]["mode"] == "passthrough"
    assert "passthrough" in sent["verifier"]["stdout"]
    assert sent["final_event"]["kind"] == KIND_FINAL
    assert sent["final_event"]["controller_result"] == controller_result
    if controller_result == "timed_out":
        assert sent["final_event"]["status"] == "timed_out"
    assert SENTINEL_SECRET.decode() not in json.dumps(sent)


def test_unknown_registration_invokes_zero_verifiers(monkeypatch, tmp_path):
    _callback_env(monkeypatch, tmp_path)
    root = _outbox_env(monkeypatch, tmp_path)
    path = _task(tmp_path)
    calls = {"verifier": 0}

    def runner(argv, event_bytes, timeout):
        result = receive_event_bytes(event_bytes)
        return CompletedProcess(argv, result.code, (result.reason + "\n").encode(), b"")

    def verifier(*_args, **_kwargs):
        calls["verifier"] += 1
        raise AssertionError("unknown task must not spend a verifier turn")

    outbox.ssh_runner = runner
    outbox.verifier_runner = verifier
    try:
        assert outbox.main(
            ["enqueue", "--task-json", str(path), "--controller-result", "failed", "--controller-detail", "nope"]
        ) == 0
        assert outbox.main(["drain"]) == 0
    finally:
        outbox.ssh_runner = None
        outbox.verifier_runner = None
    assert calls["verifier"] == 0
    failed = json.loads(next((root / "failed").glob("*.json")).read_text(encoding="utf-8"))
    assert failed["last_rc"] == 2
    assert failed["last_reason"] == "unknown_task"
    assert failed["probe_status"] == "unknown"
    assert failed.get("verifier") is None
    assert list((root / "sent").glob("*.json")) == []


def test_passthrough_result_survives_restart_and_transport_retry(monkeypatch, tmp_path):
    _callback_env(monkeypatch, tmp_path)
    root = _outbox_env(monkeypatch, tmp_path)
    _register(tmp_path)
    path = _task(tmp_path)
    calls = {"verifier": 0, "ssh": 0}

    def runner(argv, event_bytes, timeout):
        calls["ssh"] += 1
        event = json.loads(event_bytes.decode("utf-8"))
        if event["kind"] == KIND_PROBE:
            result = receive_event_bytes(event_bytes)
            return CompletedProcess(argv, result.code, (result.reason + "\n").encode(), b"")
        if calls["ssh"] == 2:
            return CompletedProcess(argv, 1, b"transport_error\n", b"down")
        result = receive_event_bytes(event_bytes)
        return CompletedProcess(argv, result.code, (result.reason + "\n").encode(), b"")

    def verifier(argv, prompt, workspace, timeout):
        calls["verifier"] += 1
        return CompletedProcess(argv, 0, "凍結驗收輸出\n".encode(), b"")

    outbox.ssh_runner = runner
    outbox.verifier_runner = verifier
    try:
        assert outbox.main(["enqueue", "--task-json", str(path), "--controller-result", "complete"]) == 0
        assert outbox.main(["drain"]) == 0
        pending = list((root / "pending").glob("*.json"))
        assert len(pending) == 1
        record = json.loads(pending[0].read_text(encoding="utf-8"))
        assert record["verifier"]["mode"] == "passthrough"
        assert "passthrough" in record["final_event"]["verifier_stdout"]
        assert calls["verifier"] == 0
        record["next_ok_at"] = 0
        pending[0].write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        assert outbox.main(["drain"]) == 0
    finally:
        outbox.ssh_runner = None
        outbox.verifier_runner = None
    assert calls["verifier"] == 0
    sent = json.loads(next((root / "sent").glob("*.json")).read_text(encoding="utf-8"))
    assert "passthrough" in sent["final_event"]["verifier_stdout"]
    assert list((root / "pending").glob("*.json")) == []
    assert outbox.main(["drain"]) == 0
    assert calls["verifier"] == 0


def test_path_ssh_and_fake_verifier_executable(monkeypatch, tmp_path):
    _callback_env(monkeypatch, tmp_path)
    root = _outbox_env(monkeypatch, tmp_path)
    _register(tmp_path)
    path = _task(tmp_path)
    (tmp_path / "handoff.md").write_text("x", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$0\" \"$@\" >> \"$FAKE_SSH_ARGV\"\n"
        "cat > \"$FAKE_SSH_STDIN\"\n"
        "kind=$(python3 -c 'import json,sys; print(json.load(sys.stdin)[\"kind\"])' < \"$FAKE_SSH_STDIN\")\n"
        "if [ \"$kind\" = registration_probe ]; then echo registered; exit 0; fi\n"
        "echo accepted\n"
        "exit 0\n",
        encoding="utf-8",
    )
    ssh.chmod(0o755)
    fake_codex = bin_dir / "fake-codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$0\" \"$@\" > \"$FAKE_CODEX_ARGV\"\n"
        "pwd > \"$FAKE_CODEX_CWD\"\n"
        "echo 驗收通過\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_SSH_ARGV", str(tmp_path / "ssh.argv"))
    monkeypatch.setenv("FAKE_SSH_STDIN", str(tmp_path / "ssh.stdin"))
    monkeypatch.setenv("FAKE_CODEX_ARGV", str(tmp_path / "codex.argv"))
    monkeypatch.setenv("FAKE_CODEX_CWD", str(tmp_path / "codex.cwd"))
    monkeypatch.setenv(
        "HMS_CALLBACK_VERIFIER_ARGV",
        json.dumps([str(fake_codex), "exec", "-C", str(tmp_path / "workspace"), "--ephemeral"]),
    )
    outbox.ssh_runner = None
    outbox.verifier_runner = None
    assert outbox.main(["enqueue", "--task-json", str(path), "--controller-result", "complete"]) == 0
    assert outbox.main(["drain"]) == 0
    argv_text = (tmp_path / "ssh.argv").read_text(encoding="utf-8")
    assert "BatchMode=yes" in argv_text
    assert "snote-internal-ssh" in argv_text
    assert SENTINEL_SECRET.decode() not in argv_text
    assert not (tmp_path / "codex.argv").exists()
    sent = list((root / "sent").glob("*.json"))
    assert len(sent) == 1
    payload = json.loads(sent[0].read_text(encoding="utf-8"))
    assert payload["verifier"]["mode"] == "passthrough"


def test_canonical_hmac_matches_relay_helper():
    event = {
        "schema_version": 1,
        "kind": KIND_FINAL,
        "event_id": str(uuid.uuid4()),
        "task_id": "t",
        "status": "done",
        "controller_result": "complete",
        "controller_detail": "ok",
        "occurred_at": "2026-10-28T03:54:00Z",
        "handoff_path": "/private/tmp/a.md",
        "handoff_sha256": None,
        "handoff_size": 1,
        "handoff_snapshot": None,
        "artifact": None,
        "verifier_stdout": "ok",
        "verifier_returncode": 0,
    }
    assert outbox.sign_event(event, SENTINEL_SECRET) == relay_sign(event, SENTINEL_SECRET)
    probe = {
        "schema_version": 1,
        "kind": KIND_PROBE,
        "event_id": str(uuid.uuid4()),
        "task_id": "t",
        "occurred_at": "2026-10-28T03:54:00Z",
    }
    assert outbox.sign_event(probe, SENTINEL_SECRET) == relay_sign(probe, SENTINEL_SECRET)


def test_local_receiver_argv_works_without_ssh_settings(monkeypatch, tmp_path):
    receiver = tmp_path / "receiver"
    receiver.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    receiver.chmod(0o700)
    monkeypatch.setenv(
        "HMS_CALLBACK_LOCAL_RECEIVER_ARGV",
        json.dumps([str(receiver), "/path/to/env", "/path/to/python"]),
    )
    monkeypatch.delenv("HMS_CALLBACK_SSH_CONFIG", raising=False)
    monkeypatch.delenv("HMS_CALLBACK_SSH_ALIAS", raising=False)
    monkeypatch.delenv("HMS_CALLBACK_SSH_REMOTE_CMD", raising=False)
    assert outbox.receiver_argv() == [str(receiver), "/path/to/env", "/path/to/python"]


def test_local_receiver_argv_rejects_relative_or_malformed(monkeypatch):
    monkeypatch.setenv("HMS_CALLBACK_LOCAL_RECEIVER_ARGV", '["relative"]')
    with pytest.raises(SystemExit):
        outbox.receiver_argv()
    monkeypatch.setenv("HMS_CALLBACK_LOCAL_RECEIVER_ARGV", "not-json")
    with pytest.raises(SystemExit):
        outbox.receiver_argv()


def test_fresh_verifier_argv_never_resumes(monkeypatch, tmp_path):
    _outbox_env(monkeypatch, tmp_path)
    workspace = str(tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()
    argv = outbox.build_verifier_argv(workspace)
    assert "resume" not in argv
    assert "exec" in argv
    assert workspace in argv
    prompt = outbox.build_verification_prompt(
        {
            "task_id": "t",
            "controller_result": "failed",
            "controller_detail": "boom",
            "handoff_path": "/private/tmp/a.md",
            "artifact": None,
            "report_path": "/private/tmp/r.md",
            "validation_path": "/private/tmp/v.log",
            "hmac": "should-not-appear",
        }
    )
    assert "should-not-appear" not in prompt
    assert "chat_id" not in prompt
    assert "failed" in prompt
    assert "Traditional Chinese" in prompt
    assert "untrusted evidence" in prompt


def test_final_event_redacts_controller_exception_and_verifier_stderr():
    event = outbox.build_final_event(
        task_id="task-1",
        controller_result="failed",
        controller_detail="secret controller exception",
        handoff_path="/private/tmp/handoff.md",
        handoff_sha256=None,
        handoff_size=0,
        handoff_snapshot=None,
        artifact=None,
        report_path=None,
        validation_path=None,
        verifier={"returncode": 7, "stdout": "partial success", "stderr": "secret stderr"},
        secret=SENTINEL_SECRET,
        occurred_at="2026-10-28T03:54:00Z",
    )
    assert event["controller_detail"] == "controller_result=failed"
    assert event["verifier_stderr"] == "verifier exited with code 7"
    assert "secret" not in json.dumps(event)


def test_max_drain_attempts_is_configurable(monkeypatch):
    monkeypatch.delenv("HMS_CALLBACK_MAX_ATTEMPTS", raising=False)
    assert outbox.max_drain_attempts() == 60
    monkeypatch.setenv("HMS_CALLBACK_MAX_ATTEMPTS", "12")
    assert outbox.max_drain_attempts() == 12
    monkeypatch.setenv("HMS_CALLBACK_MAX_ATTEMPTS", "0")
    with pytest.raises(SystemExit):
        outbox.max_drain_attempts()


def _write_fake_route_receiver(
    path: Path,
    *,
    log_path: Path,
    accept_task: str,
    fail_final_flag: Path | None = None,
) -> None:
    fail_line = (
        "fail_final = None\n"
        if fail_final_flag is None
        else f"fail_final = Path({str(fail_final_flag)!r})\n"
    )
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        f"log = Path({str(log_path)!r})\n"
        f"accept_task = {accept_task!r}\n"
        f"{fail_line}"
        "event = json.load(sys.stdin)\n"
        "log.parent.mkdir(parents=True, exist_ok=True)\n"
        "with log.open('a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps({'kind': event.get('kind'), 'task_id': event.get('task_id'), 'argv': sys.argv[1:]}) + '\\n')\n"
        "kind = event.get('kind')\n"
        "if kind == 'final_result' and fail_final is not None and fail_final.exists():\n"
        "    print('transport_error')\n"
        "    raise SystemExit(1)\n"
        "if event.get('task_id') != accept_task:\n"
        "    print('unknown_task')\n"
        "    raise SystemExit(2)\n"
        "if kind == 'registration_probe':\n"
        "    print('registered')\n"
        "    raise SystemExit(0)\n"
        "if kind == 'final_result':\n"
        "    print('accepted')\n"
        "    raise SystemExit(0)\n"
        "print('malformed')\n"
        "raise SystemExit(3)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _receiver_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _set_dual_routes(monkeypatch, routes: dict[str, list[str]]) -> None:
    monkeypatch.delenv("HMS_CALLBACK_LOCAL_RECEIVER_ARGV", raising=False)
    monkeypatch.setenv("HMS_CALLBACK_LOCAL_RECEIVER_ROUTES", json.dumps(routes))


def _load_one(folder: Path) -> dict:
    files = list(folder.glob("*.json"))
    assert len(files) == 1, files
    return json.loads(files[0].read_text(encoding="utf-8"))


def test_dual_routes_select_original_receiver_not_list_order(monkeypatch, tmp_path):
    root = _outbox_env(monkeypatch, tmp_path)
    path = _task(tmp_path, task_id="task-a")
    log_a = tmp_path / "recv-a.log"
    log_b = tmp_path / "recv-b.log"
    recv_a = tmp_path / "recv-a"
    recv_b = tmp_path / "recv-b"
    _write_fake_route_receiver(recv_a, log_path=log_a, accept_task="task-a")
    _write_fake_route_receiver(recv_b, log_path=log_b, accept_task="task-b")
    _set_dual_routes(
        monkeypatch,
        {
            "zzz-bot-a": [str(recv_a), "zzz-bot-a"],
            "aaa-bot-b": [str(recv_b), "aaa-bot-b"],
        },
    )
    assert outbox.main(["enqueue", "--task-json", str(path), "--controller-result", "complete"]) == 0
    assert outbox.main(["drain"]) == 0
    sent = _load_one(root / "sent")
    assert sent["selected_route_id"] == "zzz-bot-a"
    assert sent["selected_route_argv"] == [str(recv_a), "zzz-bot-a"]
    kinds_a = [row["kind"] for row in _receiver_log(log_a)]
    kinds_b = [row["kind"] for row in _receiver_log(log_b)]
    assert kinds_a == [KIND_PROBE, KIND_FINAL]
    assert kinds_b == [KIND_PROBE]
    assert SENTINEL_SECRET.decode() not in json.dumps(sent)


def test_dual_routes_b_task_only_b_when_a_probed_first_unknown(monkeypatch, tmp_path):
    root = _outbox_env(monkeypatch, tmp_path)
    path = _task(tmp_path, task_id="task-b")
    log_a = tmp_path / "recv-a.log"
    log_b = tmp_path / "recv-b.log"
    recv_a = tmp_path / "recv-a"
    recv_b = tmp_path / "recv-b"
    _write_fake_route_receiver(recv_a, log_path=log_a, accept_task="task-a")
    _write_fake_route_receiver(recv_b, log_path=log_b, accept_task="task-b")
    _set_dual_routes(
        monkeypatch,
        {
            "zzz-bot-a": [str(recv_a), "zzz-bot-a"],
            "aaa-bot-b": [str(recv_b), "aaa-bot-b"],
        },
    )
    assert outbox.main(["enqueue", "--task-json", str(path), "--controller-result", "complete"]) == 0
    assert outbox.main(["drain"]) == 0
    sent = _load_one(root / "sent")
    assert sent["selected_route_id"] == "aaa-bot-b"
    assert [row["kind"] for row in _receiver_log(log_a)] == [KIND_PROBE]
    assert [row["kind"] for row in _receiver_log(log_b)] == [KIND_PROBE, KIND_FINAL]
    assert list((root / "failed").glob("*.json")) == []


def test_dual_routes_ambiguous_and_restart_keep_selection(monkeypatch, tmp_path):
    root = _outbox_env(monkeypatch, tmp_path)
    ambiguous = _task(tmp_path, task_id="task-both")
    log_a = tmp_path / "recv-a.log"
    log_b = tmp_path / "recv-b.log"
    recv_a = tmp_path / "recv-a"
    recv_b = tmp_path / "recv-b"
    fail_flag = tmp_path / "fail-a-final"
    _write_fake_route_receiver(recv_a, log_path=log_a, accept_task="task-both")
    _write_fake_route_receiver(recv_b, log_path=log_b, accept_task="task-both")
    _set_dual_routes(
        monkeypatch,
        {
            "zzz-bot-a": [str(recv_a), "zzz-bot-a"],
            "aaa-bot-b": [str(recv_b), "aaa-bot-b"],
        },
    )
    assert outbox.main(["enqueue", "--task-json", str(ambiguous), "--controller-result", "complete"]) == 0
    assert outbox.main(["drain"]) == 0
    failed = _load_one(root / "failed")
    assert failed["last_reason"] == "ambiguous_route"
    assert failed.get("final_event") is None
    assert [row["kind"] for row in _receiver_log(log_a)] == [KIND_PROBE]
    assert [row["kind"] for row in _receiver_log(log_b)] == [KIND_PROBE]

    for pending in (root / "pending").glob("*.json"):
        pending.unlink()
    for failed_path in (root / "failed").glob("*.json"):
        failed_path.unlink()
    log_a.write_text("", encoding="utf-8")
    log_b.write_text("", encoding="utf-8")

    _write_fake_route_receiver(
        recv_a, log_path=log_a, accept_task="task-a", fail_final_flag=fail_flag
    )
    _write_fake_route_receiver(recv_b, log_path=log_b, accept_task="task-b")
    fail_flag.write_text("1", encoding="utf-8")
    path = _task(tmp_path, task_id="task-a")
    assert outbox.main(["enqueue", "--task-json", str(path), "--controller-result", "complete"]) == 0
    assert outbox.main(["drain"]) == 0
    pending_records = list((root / "pending").glob("*.json"))
    assert len(pending_records) == 1
    record = json.loads(pending_records[0].read_text(encoding="utf-8"))
    assert record["selected_route_id"] == "zzz-bot-a"
    assert record["probe_status"] == "registered"
    assert [row["kind"] for row in _receiver_log(log_a)] == [KIND_PROBE, KIND_FINAL]
    assert [row["kind"] for row in _receiver_log(log_b)] == [KIND_PROBE]
    record["next_ok_at"] = 0
    pending_records[0].write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert outbox.main(["drain"]) == 0
    still_pending = json.loads(pending_records[0].read_text(encoding="utf-8"))
    assert still_pending["selected_route_id"] == "zzz-bot-a"
    assert [row["kind"] for row in _receiver_log(log_b)] == [KIND_PROBE]
    fail_flag.unlink()
    still_pending["next_ok_at"] = 0
    pending_records[0].write_text(json.dumps(still_pending, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert outbox.main(["drain"]) == 0
    sent = _load_one(root / "sent")
    assert sent["selected_route_id"] == "zzz-bot-a"
    assert [row["kind"] for row in _receiver_log(log_b)] == [KIND_PROBE]
    assert [row["kind"] for row in _receiver_log(log_a)].count(KIND_FINAL) == 3


def test_dual_routes_config_change_and_malformed_fail_closed(monkeypatch, tmp_path):
    root = _outbox_env(monkeypatch, tmp_path)
    path = _task(tmp_path, task_id="task-a")
    log_a = tmp_path / "recv-a.log"
    log_b = tmp_path / "recv-b.log"
    recv_a = tmp_path / "recv-a"
    recv_b = tmp_path / "recv-b"
    recv_a2 = tmp_path / "recv-a2"
    fail_flag = tmp_path / "fail-final"
    fail_flag.write_text("1", encoding="utf-8")
    _write_fake_route_receiver(recv_a, log_path=log_a, accept_task="task-a", fail_final_flag=fail_flag)
    _write_fake_route_receiver(recv_b, log_path=log_b, accept_task="task-b")
    _write_fake_route_receiver(recv_a2, log_path=log_a, accept_task="task-a")
    _set_dual_routes(
        monkeypatch,
        {
            "zzz-bot-a": [str(recv_a), "zzz-bot-a"],
            "aaa-bot-b": [str(recv_b), "aaa-bot-b"],
        },
    )
    assert outbox.main(["enqueue", "--task-json", str(path), "--controller-result", "complete"]) == 0
    assert outbox.main(["drain"]) == 0
    pending = list((root / "pending").glob("*.json"))
    assert len(pending) == 1
    record = json.loads(pending[0].read_text(encoding="utf-8"))
    assert record["selected_route_id"] == "zzz-bot-a"
    _set_dual_routes(
        monkeypatch,
        {
            "zzz-bot-a": [str(recv_a2), "zzz-bot-a"],
            "aaa-bot-b": [str(recv_b), "aaa-bot-b"],
        },
    )
    record["next_ok_at"] = 0
    pending[0].write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert outbox.main(["drain"]) == 0
    failed = _load_one(root / "failed")
    assert failed["last_reason"] == "route_changed"
    assert failed.get("phase") != "sent"
    assert [row["kind"] for row in _receiver_log(log_b)] == [KIND_PROBE]

    planted = SENTINEL_SECRET.decode()
    monkeypatch.setenv("HMS_CALLBACK_LOCAL_RECEIVER_ROUTES", planted + "{not-json")
    with pytest.raises(SystemExit) as exc:
        outbox.configured_receiver_routes()
    assert exc.value.args == ("misconfigured",)
    assert planted not in str(exc.value)
    monkeypatch.setenv("HMS_CALLBACK_LOCAL_RECEIVER_ROUTES", "{}")
    with pytest.raises(SystemExit) as exc:
        outbox.configured_receiver_routes()
    assert exc.value.args == ("misconfigured",)
    monkeypatch.setenv(
        "HMS_CALLBACK_LOCAL_RECEIVER_ARGV",
        json.dumps([str(recv_a), "zzz-bot-a"]),
    )
    monkeypatch.setenv(
        "HMS_CALLBACK_LOCAL_RECEIVER_ROUTES",
        json.dumps({"zzz-bot-a": [str(recv_a), "zzz-bot-a"]}),
    )
    with pytest.raises(SystemExit) as exc:
        outbox.configured_receiver_routes()
    assert exc.value.args == ("misconfigured",)


def test_dual_routes_real_cli_boundary_selects_and_continues(monkeypatch, tmp_path):
    root = _outbox_env(monkeypatch, tmp_path)
    src = Path(__file__).resolve().parents[1] / "src"
    wrapper_src = Path(__file__).resolve().parents[1] / "deploy" / "callback-receive-local.sh.example"
    wrapper = tmp_path / "callback-receive-local.sh"
    shutil.copy(wrapper_src, wrapper)
    wrapper.chmod(0o700)
    secret = tmp_path / "hms.secret"
    shared_secret = tmp_path / "callback.secret"
    shared_secret.write_bytes(SENTINEL_SECRET)

    def bot_bundle(name: str, token: str, chat_id: int) -> dict:
        bot_root = tmp_path / name
        state = bot_root / "callback-state"
        ws = bot_root / "ws"
        queue = bot_root / "watchdog-queue"
        for folder in ("pending", "running", "done", "failed"):
            (queue / folder).mkdir(parents=True, exist_ok=True)
        ws.mkdir(parents=True)
        env_file = bot_root / "bot.env"
        env_map = {
            "PYTHONPATH": str(src),
            "TGR_BACKGROUND_HANDOFF": "1",
            "TGR_BACKGROUND_HANDOFF_STATE_ROOT": str(state),
            "TGR_BACKGROUND_HANDOFF_SECRET_FILE": str(shared_secret),
            "TGR_BACKGROUND_HANDOFF_MAX_SKEW_SECONDS": "86400",
            "TGR_BACKGROUND_HANDOFF_QUEUE_ROOT": str(queue),
            "TGR_DEFAULT_WORKSPACE": str(ws),
            "TGR_SESSION_DB": str(bot_root / "sessions.sqlite3"),
            "TELEGRAM_BOT_TOKEN": token,
        }
        env_file.write_text(
            "".join(f"{key}={value}\n" for key, value in env_map.items()),
            encoding="utf-8",
        )
        return {"env": env_map, "env_file": env_file, "state": state, "db": bot_root / "sessions.sqlite3", "token": token, "chat_id": chat_id}

    bot_a = bot_bundle("bot-a", "111:TOKEN-A", 7)
    bot_b = bot_bundle("bot-b", "222:TOKEN-B", 8)
    handoff_a = tmp_path / "handoff.md"
    handoff_b = tmp_path / "handoff-b.md"
    handoff_a.write_text("worker handoff A", encoding="utf-8")
    handoff_b.write_text("worker handoff B", encoding="utf-8")
    msg_a = FakeMessage(text="hi", chat_id=7, message_id=1)
    msg_a.from_user = SimpleNamespace(id=99)
    msg_b = FakeMessage(text="hi", chat_id=8, message_id=2)
    msg_b.from_user = SimpleNamespace(id=88)
    register_from_relay_context(
        task_id="task-a",
        handoff_path=str(handoff_a),
        msg=msg_a,
        thread_key="private:7",
        env=bot_a["env"],
        session_id="sess-a",
    )
    register_from_relay_context(
        task_id="task-b",
        handoff_path=str(handoff_b),
        msg=msg_b,
        thread_key="private:8",
        env=bot_b["env"],
        session_id="sess-b",
    )
    _set_dual_routes(
        monkeypatch,
        {
            "zzz-bot-a": [str(wrapper), str(bot_a["env_file"]), sys.executable],
            "aaa-bot-b": [str(wrapper), str(bot_b["env_file"]), sys.executable],
        },
    )
    path = _task(tmp_path, task_id="task-a")
    outbox.ssh_runner = None
    outbox.verifier_runner = None
    assert outbox.main(["enqueue", "--task-json", str(path), "--controller-result", "complete"]) == 0
    assert outbox.main(["drain"]) == 0
    sent = _load_one(root / "sent")
    assert sent["selected_route_id"] == "zzz-bot-a"
    inbox_a = list((bot_a["state"] / "inbox" / "new").glob("*.json"))
    inbox_b = list((bot_b["state"] / "inbox" / "new").glob("*.json"))
    assert len(inbox_a) == 1
    assert inbox_b == []
    event = json.loads(inbox_a[0].read_text(encoding="utf-8"))
    assert event["kind"] == KIND_FINAL
    assert event["task_id"] == "task-a"
    scheduled: list[tuple[str, str]] = []
    delivered: list[str] = []

    async def schedule(reg, ev):
        sid = frozen_codex_session_id(reg)
        DurableQueue(bot_a["db"]).add(
            thread_key=str(reg["thread_key"]),
            backend="codex",
            prompt="Relay background-worker continuation. fixture",
            kind=CONTINUATION_KIND,
            chat_id=int(reg["chat_id"]),
            message_id=0,
            topic_id=None,
            task_id=str(ev["task_id"]),
            event_id=str(ev["event_id"]),
            resume_session_id=sid,
        )
        scheduled.append((sid, str(ev["event_id"])))

    async def deliver(_reg, text):
        delivered.append(text)

    asyncio.run(
        scan_once(
            active_thread_keys=set(),
            deliver=deliver,
            schedule_continuation=schedule,
            env=bot_a["env"],
        )
    )
    asyncio.run(
        scan_once(
            active_thread_keys=set(),
            deliver=deliver,
            schedule_continuation=lambda *_: (_ for _ in ()).throw(AssertionError("bot-b must stay empty")),
            env=bot_b["env"],
        )
    )
    assert scheduled == [("sess-a", event["event_id"])]
    assert delivered == []
    row = DurableQueue(bot_a["db"]).lookup_event(event["event_id"])
    assert row is not None
    assert row.resume_session_id == "sess-a"
    assert DurableQueue(bot_b["db"]).lookup_event(event["event_id"]) is None
    fp_a = bot_fingerprint_from_env(bot_a["env"])
    fp_b = bot_fingerprint_from_env(bot_b["env"])
    assert fp_a != fp_b
    assert hashlib.sha256(b"111:TOKEN-A").hexdigest()[:12] == fp_a
    assert list((bot_b["state"] / "inbox" / "new").glob("*.json")) == []
    assert secret.read_bytes() == SENTINEL_SECRET
    assert SENTINEL_SECRET.decode() not in json.dumps(sent)
