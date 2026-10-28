"""HMS standalone outbox: probe, fresh HMS verifier, durable final-result."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from tg_cli_relay.background_callback import (
    KIND_FINAL,
    KIND_PROBE,
    receive_event_bytes,
    register_from_relay_context,
    sign_event as relay_sign,
)
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
def test_each_terminal_invokes_one_hms_verifier_after_probe(monkeypatch, tmp_path, controller_result):
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
    assert captured["verifier"] == 1
    sent = json.loads(next((root / "sent").glob("*.json")).read_text(encoding="utf-8"))
    assert sent["verifier"]["stdout"].startswith("獨立驗收：通過")
    assert sent["final_event"]["kind"] == KIND_FINAL
    assert sent["final_event"]["controller_result"] == controller_result
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


def test_verifier_result_survives_restart_and_transport_retry(monkeypatch, tmp_path):
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
        assert record["verifier"]["stdout"].startswith("凍結驗收輸出")
        assert record["final_event"]["verifier_stdout"].startswith("凍結驗收輸出")
        assert calls["verifier"] == 1
        record["next_ok_at"] = 0
        pending[0].write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        assert outbox.main(["drain"]) == 0
    finally:
        outbox.ssh_runner = None
        outbox.verifier_runner = None
    assert calls["verifier"] == 1
    sent = json.loads(next((root / "sent").glob("*.json")).read_text(encoding="utf-8"))
    assert sent["final_event"]["verifier_stdout"].startswith("凍結驗收輸出")
    assert list((root / "pending").glob("*.json")) == []
    assert outbox.main(["drain"]) == 0
    assert calls["verifier"] == 1


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
    codex_tokens = [line.strip() for line in (tmp_path / "codex.argv").read_text(encoding="utf-8").splitlines()]
    assert "exec" in codex_tokens
    assert "resume" not in set(codex_tokens)
    assert str(tmp_path / "workspace") in codex_tokens
    assert SENTINEL_SECRET.decode() not in "\n".join(codex_tokens)
    cwd = (tmp_path / "codex.cwd").read_text(encoding="utf-8").strip()
    assert Path(cwd).resolve() == (tmp_path / "workspace").resolve()
    sent = list((root / "sent").glob("*.json"))
    assert len(sent) == 1


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
