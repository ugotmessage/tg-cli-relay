# tg-cli-relay

在 **Telegram（之後也可換成其他觸發來源）** 與 **本機 CLI 編碼代理** 之間做中繼：統一處理「對話 thread 怎麼定義」、「各後端的 session / 歷史怎麼接續」，後端不限於 **Cursor `agent`**，也包含 **OpenAI `codex`**。

## 核心概念

### 1) Thread key（外部對話的唯一鍵）

建議用來對應「同一串聊天」：

| 情境 | Thread key 組成 |
|------|------------------|
| 私聊 | `private:{chat_id}` |
| 群組 | `group:{chat_id}` |
| 論壇話題 | `topic:{chat_id}:{message_thread_id}` |

程式內以字串儲存；之後不論走 Cursor 或 Codex，都用同一個 key 查「該 thread 在各後端的 session id」。

### 2) 後端 session（各 CLI 自己的對話室）

- **Cursor**：`agent create-chat` 取得 UUID → 之後每次 `agent --print --resume <uuid> ...` 接續。多人服務時不要用 `--continue` 當主流程，避免搶到別人的「上一個 session」。
- **Codex**：第一次用 `codex exec`（預設會落地 session）；後續用 `codex exec resume <SESSION_ID> ...`。也可用 `codex exec --json` 讓 stdout 變成 JSONL，方便腳本解析事件與取得 session id（實際欄位請以你安裝版本輸出為準）。

### 3) 自己要不要存「全文歷史」

- **最小做法**：只存 `thread_key → session_id` 對照表，上下文交給各 CLI。
- **加強做法**：另存最近 N 則、摘要、審計 log；當 session 遺失或要遷移時，用摘要 + 新 session 重建。

## 目錄說明

```
src/tg_cli_relay/     共用邏輯（SQLite 對照、subprocess 包裝）
deploy/               systemd 範例（背景常駐）
```

## 環境變數

複製 `env.example` 為 `.env` 後載入（或直接用 export）：

- `TGR_SESSION_DB`：SQLite 路徑，預設 `./data/sessions.sqlite3`
- `TGR_DEFAULT_WORKSPACE`：預設工作目錄（git 專案根）
- `TGR_CURSOR_AGENT_BIN`：Cursor Agent CLI，預設 `agent`
- `TGR_CODEX_BIN`：OpenAI **Codex** CLI，預設 `codex`；僅在 `TGR_BACKEND=codex` 時會用到（與 Cursor 無關）

**Cursor 認證**：常見為在執行 bot 的同一使用者環境下執行過 **`agent login`**（OAuth／瀏覽器登入）；亦可依官方文件使用 **`CURSOR_API_KEY`**（腳本／CI 較常見）。常駐服務請確認 systemd 的 `User=` 與你當初 `agent login` 的是同一帳號與家目錄，否則讀不到已登入狀態。

**Codex 認證**：`codex login` 或該工具支援的設定檔／環境變數。

## 安裝（可編輯模式）

```bash
cd /srv/tg-cli-relay
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[telegram]"
```

開發時若尚未 `pip install -e .`，可暫用：

```bash
PYTHONPATH=src python3 -m tg_cli_relay doctor
```

## CLI 小工具（本專案）

```bash
python3 -m tg_cli_relay --help
python3 -m tg_cli_relay doctor
python3 -m tg_cli_relay run cursor 'private:123456' '幫我檢查這個 repo 的 README'
python3 -m tg_cli_relay bot
```

## 背景執行

參考 `deploy/tg-cli-relay.service.example`：以 systemd 固定 `User`、`WorkingDirectory`、`EnvironmentFile`，並把 `PATH` 指到裝有 `agent` / `codex` 的位置。

## 免責與安全

- Bot Token、API key、可寫入的工作目錄都屬於高風險權限；務必限制誰能對機器人下指令（例如白名單 `user_id`）。
- `codex exec` 的 `--dangerously-bypass-approvals-and-sandbox` 與 Cursor 的 `--yolo` / 寬鬆權限模式僅適合已在外層隔離的環境。
