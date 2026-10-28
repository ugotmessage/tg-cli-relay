"""HMS standalone outbox: canonical HMAC, argv ssh, durable sent receipt."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

from tg_cli_relay.background_callback import receive_event_bytes, register_from_relay_context, sign_event as relay_sign
from test_task_isolation import FakeMessage
from test_background_callback import SENTINEL_SECRET, _callback_env

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import hms_callback_outbox as outbox  # noqa: E402


def _task(tmp_path: Path, *, enabled: bool = True) -> Path:
    handoff = tmp_path / "handoff.md"
    handoff.write_text("worker handoff", encoding="utf-8")
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    payload = {
        "id": "task-outbox-1",
        "callback": {
            "enabled": enabled,
            "handoff_path": str(handoff),
            "artifact": str(artifact),
        },
    }
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


def test_enqueue_disabled_is_noop(monkeypatch, tmp_path):
    _outbox_env(monkeypatch, tmp_path)
    path = _task(tmp_path, enabled=False)
    assert outbox.main(["enqueue", "--task-json", str(path), "--controller-result", "complete"]) == 0
    assert list((tmp_path / "outbox" / "pending").glob("*.json")) == []


def test_sender_hmac_accepted_by_receiver(monkeypatch, tmp_path):
    _callback_env(monkeypatch, tmp_path)
    root = _outbox_env(monkeypatch, tmp_path)
    msg = FakeMessage(text="hi", chat_id=7, message_id=1)
    msg.from_user = SimpleNamespace(id=99)
    register_from_relay_context(
        task_id="task-outbox-1",
        handoff_path=str(tmp_path / "handoff.md"),
        msg=msg,
        thread_key="private:7",
    )
    (tmp_path / "handoff.md").write_text("worker handoff", encoding="utf-8")
    path = _task(tmp_path)
    captured: dict = {}

    def runner(argv, event_bytes, timeout):
        captured["argv"] = argv
        captured["stdin"] = event_bytes
        captured["timeout"] = timeout
        event = json.loads(event_bytes.decode("utf-8"))
        assert event["hmac"] == relay_sign(event, SENTINEL_SECRET)
        result = receive_event_bytes(event_bytes)
        stdout = (result.reason + "\n").encode("utf-8")
        return CompletedProcess(argv, result.code, stdout, b"")

    outbox.ssh_runner = runner
    try:
        assert outbox.main(
            ["enqueue", "--task-json", str(path), "--controller-result", "complete", "--controller-detail", "ok"]
        ) == 0
        pending = list((root / "pending").glob("*.json"))
        assert len(pending) == 1
        assert outbox.main(["drain"]) == 0
    finally:
        outbox.ssh_runner = None
    assert captured["argv"] == [
        "ssh",
        "-F",
        str(tmp_path / "ssh-config"),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "snote-internal-ssh",
        "/usr/local/sbin/tg-cli-relay-callback-receive",
    ]
    assert captured["stdin"].endswith(b"\n")
    assert SENTINEL_SECRET not in captured["stdin"]
    assert b"SEN_TEST_SECRET" not in captured["argv"][2].encode()
    sent = list((root / "sent").glob("*.json"))
    assert len(sent) == 1
    assert list((root / "pending").glob("*.json")) == []
    assert outbox.main(["drain"]) == 0
    assert len(list((root / "sent").glob("*.json"))) == 1


def test_unknown_task_is_durable_unregistered_outcome(monkeypatch, tmp_path):
    _callback_env(monkeypatch, tmp_path)
    root = _outbox_env(monkeypatch, tmp_path)
    path = _task(tmp_path)
    (tmp_path / "handoff.md").write_text("x", encoding="utf-8")

    def runner(argv, event_bytes, timeout):
        result = receive_event_bytes(event_bytes)
        return CompletedProcess(argv, result.code, (result.reason + "\n").encode(), b"")

    outbox.ssh_runner = runner
    try:
        assert outbox.main(["enqueue", "--task-json", str(path), "--controller-result", "failed", "--controller-detail", "nope"]) == 0
        assert outbox.main(["drain"]) == 0
    finally:
        outbox.ssh_runner = None
    failed = json.loads(next((root / "failed").glob("*.json")).read_text(encoding="utf-8"))
    assert failed["last_rc"] == 2
    assert failed["last_reason"] == "unknown_task"
    assert list((root / "sent").glob("*.json")) == []


def test_path_ssh_executable_gets_stdin(monkeypatch, tmp_path):
    root = _outbox_env(monkeypatch, tmp_path)
    path = _task(tmp_path)
    (tmp_path / "handoff.md").write_text("x", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$0\" \"$@\" > \"$FAKE_SSH_ARGV\"\n"
        "cat > \"$FAKE_SSH_STDIN\"\n"
        "echo accepted\n"
        "exit 0\n",
        encoding="utf-8",
    )
    ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_SSH_ARGV", str(tmp_path / "ssh.argv"))
    monkeypatch.setenv("FAKE_SSH_STDIN", str(tmp_path / "ssh.stdin"))
    outbox.ssh_runner = None
    assert outbox.main(["enqueue", "--task-json", str(path), "--controller-result", "complete"]) == 0
    assert outbox.main(["drain"]) == 0
    argv_text = (tmp_path / "ssh.argv").read_text(encoding="utf-8")
    assert "BatchMode=yes" in argv_text
    assert "snote-internal-ssh" in argv_text
    stdin = (tmp_path / "ssh.stdin").read_bytes()
    event = json.loads(stdin.decode("utf-8"))
    assert event["hmac"] == relay_sign(event, SENTINEL_SECRET)
    assert (root / "sent" / f"{event['event_id']}.json").is_file()


def test_canonical_hmac_matches_relay_helper():
    event = {
        "schema_version": 1,
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
    }
    assert outbox.sign_event(event, SENTINEL_SECRET) == relay_sign(event, SENTINEL_SECRET)
