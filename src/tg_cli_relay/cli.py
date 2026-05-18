from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from tg_cli_relay.relay import relay_turn
from tg_cli_relay.session_store import SessionStore


def _load_dotenv() -> None:
    path = Path(".env")
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    p = argparse.ArgumentParser(prog="tg-cli-relay")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="對單一 thread_key 送出一輪 prompt（Cursor 或 Codex）")
    run.add_argument("backend", choices=["cursor", "codex", "claude"])
    run.add_argument("thread_key", help="例如 private:123456")
    run.add_argument("prompt", help="要送給代理的文字")
    run.add_argument("--workspace", help="覆寫 TGR_DEFAULT_WORKSPACE")

    sub.add_parser("doctor", help="檢查環境變數與 session 資料庫路徑")

    sub.add_parser("bot", help="啟動 Telegram bot（需安裝 extras: telegram）")

    args = p.parse_args(argv)
    if args.cmd == "doctor":
        db = os.environ.get("TGR_SESSION_DB", str(Path("data") / "sessions.sqlite3"))
        ws = os.environ.get("TGR_DEFAULT_WORKSPACE", "")
        print("TGR_SESSION_DB =", db)
        print("TGR_DEFAULT_WORKSPACE =", ws or "(未設定)")
        print("TGR_CURSOR_AGENT_BIN =", os.environ.get("TGR_CURSOR_AGENT_BIN", "agent"))
        print("TGR_CODEX_BIN =", os.environ.get("TGR_CODEX_BIN", "codex"))
        print("TGR_CLAUDE_BIN =", os.environ.get("TGR_CLAUDE_BIN", "claude"))
        skip = os.environ.get("TGR_CLAUDE_SKIP_PERMISSIONS", "").strip().lower() in ("1", "true", "yes")
        print("TGR_CLAUDE_SKIP_PERMISSIONS =", "已啟用（--dangerously-skip-permissions）" if skip else "未啟用")
        print("TELEGRAM_BOT_TOKEN =", "(已設定)" if os.environ.get("TELEGRAM_BOT_TOKEN") else "(未設定)")
        print("TGR_BACKEND =", os.environ.get("TGR_BACKEND", "cursor"))
        return 0

    if args.cmd == "bot":
        from tg_cli_relay.telegram_bot import run_bot

        run_bot()
        return 0

    assert args.cmd == "run"
    store = SessionStore(Path(os.environ.get("TGR_SESSION_DB", str(Path("data") / "sessions.sqlite3"))))
    res = relay_turn(
        thread_key=args.thread_key,
        backend=args.backend,
        prompt=args.prompt,
        store=store,
        workspace=args.workspace,
    )
    if res.stdout:
        sys.stdout.write(res.stdout)
    if res.stderr:
        sys.stderr.write(res.stderr)
    return 0 if res.returncode == 0 else res.returncode


if __name__ == "__main__":
    raise SystemExit(main())
