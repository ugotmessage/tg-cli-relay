# tg-cli-relay

透過 **Telegram** 對本機 **CLI 編碼代理**下指令的中繼橋接器。統一處理對話 thread 的定義、各後端的 session 接續，以及常用 bot 指令（重置、切換模型等）。

支援後端：**Cursor `agent`**、**OpenAI `codex`**、**Claude Code `claude`**

---

## 架構概覽

```
Telegram 訊息
    │
    ▼
telegram_bot.py   ← 解析 thread key、處理指令、顯示回覆
    │
    ▼
relay.py          ← 查 session、呼叫 provider、存回 session
    │
    ▼
providers/        ← 各 CLI 的 subprocess 包裝
    │
    ▼
SQLite            ← sessions 表（thread_key → session_id）
                     preferences 表（thread_key → model 等偏好）
```

**歷史紀錄由各 CLI 本身維護**；本專案只存 session ID，不存對話內容。

---

## 支援後端

| 後端 | CLI | Session 接續方式 | Model flag |
|------|-----|-----------------|-----------|
| `cursor` | `agent` | `agent create-chat` → `--resume <id>` | `--model` |
| `codex` | `codex` | `codex exec` → `codex exec resume <id>` | `-m` |
| `claude` | `claude` | `claude --resume <id>` | `--model` |

---

## Telegram 指令

| 指令 | 後端 | 說明 |
|------|------|------|
| `/reset` `/new` | 全部 | 清除 session，下一則訊息開啟新對話 |
| `/status` | 全部 | 顯示後端、工作目錄、session 狀態 |
| `/model` | 全部 | 列出常用模型清單 |
| `/model <id>` | 全部 | 切換模型（下一輪起生效） |
| `/help` | 全部 | 顯示可用指令 |

> `/model` 對 Claude 有嚴格驗證；Cursor 和 Codex 為 pass-through（由 CLI 本身回報錯誤）。

---

## 安裝

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[telegram]"
```

---

## 環境變數

複製 `env.example` 為 `.env`：

```bash
cp env.example .env
```

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `TGR_BACKEND` | `cursor` | 使用的後端：`cursor` / `codex` / `claude` |
| `TGR_SESSION_DB` | `./data/sessions.sqlite3` | SQLite 路徑 |
| `TGR_DEFAULT_WORKSPACE` | （必填） | 代理操作的 git 工作目錄 |
| `TGR_CURSOR_AGENT_BIN` | `agent` | Cursor Agent CLI 路徑 |
| `TGR_CODEX_BIN` | `codex` | OpenAI Codex CLI 路徑 |
| `TGR_CLAUDE_BIN` | `claude` | Claude Code CLI 路徑 |
| `TGR_CLAUDE_SKIP_PERMISSIONS` | （未設定） | 設為 `1` 啟用 `--dangerously-skip-permissions` |
| `TELEGRAM_BOT_TOKEN` | （必填） | BotFather 取得的 token |
| `TGR_ALLOWED_TELEGRAM_USER_IDS` | （留空不限制） | 允許使用的 Telegram user ID，逗號分隔 |

---

## 認證

### Cursor
- **OAuth（建議）**：在執行 bot 的同一使用者環境下執行 `agent login`，憑證存於家目錄。systemd 的 `User=` 必須與登入帳號一致。
- **API Key**：設定 `CURSOR_API_KEY` 環境變數。

### Codex
執行 `codex login` 或依 CLI 文件設定 API Key。

### Claude
兩種方式擇一：

| 方式 | 步驟 | 適合情境 |
|------|------|---------|
| **OAuth** | 執行 `claude login`（瀏覽器）；憑證存於 Keychain | 個人使用、Claude.ai 訂閱 |
| **API Key** | 設定 `ANTHROPIC_API_KEY=sk-ant-...` | Server 部署、無人值守（建議） |

部署範本參考 `deploy/env.claude.example`。

> **macOS 注意**：Claude Code OAuth 憑證存放在系統 Keychain。請勿將 `oat01-` 格式的 OAuth token 設為 `ANTHROPIC_API_KEY`（例如透過 `launchctl setenv` 全域注入）——claude CLI 會把它當 API key 呼叫，導致 "Invalid API key" 錯誤。本專案的 `claude_cli.py` 會自動偵測並移除 `oat01-` 格式的值，讓 CLI 回到 Keychain 路徑；真正的 API key（`sk-ant-` 開頭）不受影響。

---

## 使用

```bash
# 檢查環境設定
python3 -m tg_cli_relay doctor

# 單次測試（不啟動 bot）
python3 -m tg_cli_relay run claude 'private:123456' '幫我檢查這個 repo 的 README'

# 啟動 bot
python3 -m tg_cli_relay bot

# 或用腳本（Linux/macOS）
./start_bot.sh
./stop_bot.sh
```

---

## 背景常駐（macOS LaunchAgent）

參考 `deploy/ht.tg-cli-relay.plist.example`：

```bash
cp deploy/ht.tg-cli-relay.plist.example ~/Library/LaunchAgents/ht.tg-cli-relay.plist
# 編輯 ProgramArguments（python3 路徑）、WorkingDirectory、YOUR_USERNAME
launchctl load ~/Library/LaunchAgents/ht.tg-cli-relay.plist
```

**重要**：plist 的 `EnvironmentVariables` 須明確設定 `HOME`、`USER`、`LOGNAME` 與 `PATH`，LaunchAgent 預設環境很精簡，缺少這些值會導致 CLI 找不到 Keychain 憑證或可執行檔。

查看 log：

```bash
tail -f /tmp/tg-cli-relay-stderr.log
```

停用：

```bash
launchctl unload ~/Library/LaunchAgents/ht.tg-cli-relay.plist
```

---

## 背景常駐（Linux systemd）

參考 `deploy/tg-cli-relay.service.example` 設定 systemd：

```bash
sudo cp deploy/tg-cli-relay.service.example /etc/systemd/system/tg-cli-relay.service
# 編輯 User=、EnvironmentFile=、PATH 後：
sudo systemctl enable --now tg-cli-relay
```

---

## 安全注意事項

- 務必設定 `TGR_ALLOWED_TELEGRAM_USER_IDS` 限制可操作的使用者。
- `TGR_CLAUDE_SKIP_PERMISSIONS=1` 會讓 Claude CLI 跳過所有工具授權確認，僅適合已隔離的環境。
- Bot Token 與 API key 具有高風險寫入權限，請勿提交至版本控制。
