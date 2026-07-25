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
    run.add_argument("backend", choices=["cursor", "codex", "claude", "opencode"])
    run.add_argument("thread_key", help="例如 private:123456")
    run.add_argument("prompt", help="要送給代理的文字")
    run.add_argument("--workspace", help="覆寫 TGR_DEFAULT_WORKSPACE")

    sub.add_parser("doctor", help="檢查環境變數與 session 資料庫路徑")

    sub.add_parser("bot", help="啟動 Telegram bot（需安裝 extras: telegram）")

    cleanup = sub.add_parser("cleanup-uploads", help="清理超過保留期限的 Telegram 上傳暫存")
    cleanup.add_argument("--dry-run", action="store_true", help="僅顯示設定，不刪除")

    args = p.parse_args(argv)
    if args.cmd == "doctor":
        db = os.environ.get("TGR_SESSION_DB", str(Path("data") / "sessions.sqlite3"))
        ws = os.environ.get("TGR_DEFAULT_WORKSPACE", "")
        print("TGR_SESSION_DB =", db)
        print("TGR_DEFAULT_WORKSPACE =", ws or "(未設定)")
        print("TGR_CURSOR_AGENT_BIN =", os.environ.get("TGR_CURSOR_AGENT_BIN", "agent"))
        print("TGR_CODEX_BIN =", os.environ.get("TGR_CODEX_BIN", "codex"))
        bypass = os.environ.get("TGR_CODEX_BYPASS_APPROVALS_AND_SANDBOX", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        print(
            "TGR_CODEX_BYPASS_APPROVALS_AND_SANDBOX =",
            "已啟用（--dangerously-bypass-approvals-and-sandbox）" if bypass else "未啟用",
        )
        print("TGR_CLAUDE_BIN =", os.environ.get("TGR_CLAUDE_BIN", "claude"))
        skip = os.environ.get("TGR_CLAUDE_SKIP_PERMISSIONS", "").strip().lower() in ("1", "true", "yes")
        print("TGR_CLAUDE_SKIP_PERMISSIONS =", "已啟用（--dangerously-skip-permissions）" if skip else "未啟用")
        print("TGR_OPENCODE_BIN =", os.environ.get("TGR_OPENCODE_BIN", "opencode"))
        print("TELEGRAM_BOT_TOKEN =", "(已設定)" if os.environ.get("TELEGRAM_BOT_TOKEN") else "(未設定)")
        print("TGR_BACKEND =", os.environ.get("TGR_BACKEND", "cursor"))
        return 0

    if args.cmd == "bot":
        from tg_cli_relay.telegram_bot import run_bot

        run_bot()
        return 0

    if args.cmd == "cleanup-uploads":
        from tg_cli_relay.attachments import AttachmentConfig, cleanup_upload_dirs

        cfg = AttachmentConfig.from_env()
        if args.dry_run:
            print("TGR_UPLOAD_DIR =", cfg.upload_dir)
            print("TGR_UPLOAD_RETENTION_HOURS =", cfg.upload_retention_hours)
            return 0
        removed = cleanup_upload_dirs(cfg)
        print(f"已清理 {removed} 個過期 upload 目錄")
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
