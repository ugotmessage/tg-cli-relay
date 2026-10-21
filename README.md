# tg-cli-relay

透過 **Telegram** 對本機 **CLI 編碼代理**下指令的中繼橋接器。統一處理對話 thread 的定義、各後端的 session 接續，以及常用 bot 指令（重置、切換模型等）。

支援後端：**Cursor `agent`**、**OpenAI `codex`**、**Claude Code `claude`**、**OpenCode `opencode`**

---

## 架構概覽

```
Telegram 訊息
    │
    ▼
telegram_bot.py   ← 解析 thread key、處理指令、顯示回覆
    │
    ├── task_router.py  ← rule-based DIRECT / DELEGATE 判斷與 prompt 前綴
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
| `opencode` | `opencode` | `opencode run -c`（續最後一個，靠 workspace 隔離） | `-m` |

> **OpenCode 限制**：無法指定 session ID 接續，同一 workspace 下多個 Telegram thread 會共用同一個 session。建議一個 thread 對應一個 workspace。

---

## Telegram 指令

| 指令 | 後端 | 說明 |
|------|------|------|
| `/reset` `/new` | 全部 | 清除 session，下一則訊息開啟新對話 |
| `/status` | 全部 | 顯示目前 provider、工作目錄、session 狀態 |
| `/provider` | 全部 | 列出可切換的 CLI provider |
| `/provider <id>` | 全部 | 切換 provider（例如 `claude`、`codex`） |
| `/model` | 全部 | 依 provider 列出常用模型清單 |
| `/model <id>` | 全部 | 切換目前 provider 的模型（下一輪起生效） |
| `/model <provider> <id>` | 全部 | 切換 provider 並設定該 provider 的模型 |
| `/router` | `cursor` | 查看目前 Task Router 規則、設定來源與 fallback 狀態 |
| `/router test <文字>` | `cursor` | 用目前規則測試文字會被判斷為 `DIRECT` 或 `DELEGATE` |
| `/task` | 全部 | 列出本對話的孤立任務 |
| `/task <id>` | 全部 | 顯示該任務狀態（此回覆會綁定該任務，回覆它可以繼續） |
| `/task <id> <說明>` | 全部 | 把說明送到該任務的獨立 session |
| `/task new <說明>` | 全部 | 開新的孤立任務（含小型 DIRECT） |
| `/help` | 全部 | 顯示可用指令 |

> 每個 Telegram thread 會保存自己的 provider。模型偏好依 provider 分開保存（`model:cursor`、`model:claude` 等）。**Cursor relay 預設一律 `--model auto`**，除非你透過 `/model` 或 `TGR_CURSOR_MODEL` 明確指定；不會沿用舊版通用 `model` 偏好或其它程式的 CLI 指定。

### 任務隔離（task isolation）

`DELEGATE` 與 `/task new` 會建立 `{parent}:job:{task_id}` 獨立 session，來源／ACK／進度／狀態／結果都綁在該任務上。回覆綁定訊息會繼續**同一個**任務，不會掉回混在一起的父對話。細節見 `docs/task-isolation.md`。

- 未串接且整句是 `繼續`／`接著做`／`往下做`：多個任務時請選擇，**不**走父 session；只有一個明確任務時恢復該任務。
- 未串接且整句是 `進度`／`做好了嗎` 等：只回狀態，不重跑。回覆已綁定的進度查詢同樣只回狀態。
- 新的 DELEGATE（即使內文提到「進度」）仍開新任務。延續／進度用整句錨定，不會用子字串把「請繼續實作…」當成舊任務。
- **舊的未綁定父對話回覆不會自動遷移或猜測任務**。請用 `/task new <說明>` 一次建立孤立任務，之後再回覆帶 `[task …]` 的訊息。

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
| `TGR_BACKEND` | `cursor` | 使用的後端：`cursor` / `codex` / `claude` / `opencode` |
| `TGR_ENABLED_BACKENDS` | 全部支援後端 | Telegram 中可切換的 provider 清單，逗號分隔，例如 `claude,codex` |
| `TGR_SESSION_DB` | `./data/sessions.sqlite3` | SQLite 路徑 |
| `TGR_DEFAULT_WORKSPACE` | （必填） | 代理操作的 git 工作目錄 |
| `TGR_CURSOR_AGENT_BIN` | `agent` | Cursor Agent CLI 路徑 |
| `TGR_CURSOR_MODEL` | `auto` | Cursor relay 預設模型；僅影響本 relay，不受其它 CLI 呼叫或舊版 `model` 偏好影響。可用 Telegram `/model <id>` 或 `model:cursor` 覆寫 |
| `TGR_CURSOR_TIMEOUT` | `600` | Cursor agent 單輪逾時秒數；`0` 表示不限制 |
| `TGR_CURSOR_DELEGATE_TIMEOUT` | `3600` | Cursor `DELEGATE`／Codex→Cursor Worker 長工作逾時秒數；仍維持有界，避免永久卡死 |
| `TGR_ROUTER_CONFIG` | `router.toml` / `config/router.toml` | Task Router TOML 設定檔；遺失或錯誤會 fallback 至內建規則 |
| `TGR_CURSOR_HARNESS_PREFIX` | `1` | 設為 `0` 可關閉 Cursor Task Router / harness prompt 前綴 |
| `TGR_CURSOR_PROMPT_PREFIX` | （未設定） | 完全覆寫 Cursor prompt 前綴；設定後不使用 Task Router 產生的前綴 |
| `TGR_THREAD_QUEUE_MAX` | `20` | 每個 Telegram thread 忙碌時可緩存的訊息上限；FIFO 自動接續處理 |
| `TGR_CODEX_BIN` | `codex` | OpenAI Codex CLI 路徑 |
| `TGR_CODEX_BYPASS_APPROVALS_AND_SANDBOX` | （未設定） | 設為 `1` 啟用 `--dangerously-bypass-approvals-and-sandbox` |
| `TGR_CODEX_DELEGATE_TO_CURSOR` | `1` | Codex 收到 coding、實作、大量簡易或其他 `DELEGATE` 工作時，先交給獨立 Cursor Worker 執行，再由 Codex 審核；設為 `0` 關閉，Cursor 失敗會自動回退 Codex |
| `TGR_CODEX_CURSOR_RESULT_MAX_CHARS` | `24000` | 傳回 Codex 的 Cursor Worker 報告字元上限，避免 Worker 輸出耗用過多 Codex context |
| `TGR_CLAUDE_BIN` | `claude` | Claude Code CLI 路徑 |
| `TGR_CLAUDE_SKIP_PERMISSIONS` | （未設定） | 設為 `1` 啟用 `--dangerously-skip-permissions` |
| `TGR_OPENCODE_BIN` | `opencode` | OpenCode CLI 路徑 |
| `TELEGRAM_BOT_TOKEN` | （必填） | BotFather 取得的 token |
| `TGR_ALLOWED_TELEGRAM_USER_IDS` | （留空不限制） | 允許使用的 Telegram user ID，逗號分隔 |
| `TGR_UPLOAD_DIR` | `~/.hermes/tg-relay-uploads` | Telegram 附件下載暫存目錄 |
| `TGR_MAX_DOWNLOAD_BYTES` | `20971520`（20 MiB） | 單檔下載大小上限 |
| `TGR_MAX_UPLOAD_BYTES` | `20971520` | Agent 回傳附件大小上限 |
| `TGR_MAX_FILES_PER_MESSAGE` | `5` | 單次回覆最多回傳檔案數 |
| `TGR_UPLOAD_RETENTION_HOURS` | `72` | 下載暫存保留時間 |
| `TGR_ALLOWED_UPLOAD_ROOTS` | `TGR_UPLOAD_DIR` | 出站 `TGR_FILE:` 允許的根目錄，逗號分隔 |

---

## Telegram 附件

- **接收**：支援 document、photo（最高解析度）、audio、voice、video；caption 作為處理指示。
- **回傳**：Agent stdout 以獨立一行 `TGR_FILE:/absolute/path` 宣告檔案（最多 5 個）；marker 不會顯示給使用者。
- **清理**：`python3 -m tg_cli_relay cleanup-uploads`（可設 cron）；成功下載後也會做 bounded cleanup。
- 僅 **allowlist 內的 Telegram user** 可下載附件。

---

## Task Router

Cursor relay 會在送入 `agent --print` 前，用 **rule-based** Task Router 產生前綴，標示本輪應走：

- `DIRECT`：小型查詢、狀態檢查、單步操作，留在目前 Cursor session 直接處理。
- `DELEGATE`：跨檔案、重構、部署/DB/權限等高風險邊界、或「實作 + 測試 + review + PR」這類工作，要求主 session 只當 Orchestrator，透過 Job Dispatcher 拆給隔離 worker sessions，再由主 session 整合與驗證。

Router 不使用 LLM classifier；只依 TOML 規則的 `keywords` / regex `patterns` / `weight` / `threshold` 加總判斷。批次補檔會依「回補、分批、長時間監控、背景工作」等任務語意判斷，不綁定某個固定檔案數量。設定檔遺失或格式錯誤時會使用內建 fallback 規則，不會讓 bot 啟動失敗。

Codex backend 預設也使用同一份判定，遵守知識庫的「勞力活外包，決策活自己做」原則。coding／實作與大量簡易工作會先由隔離的 Cursor session 執行，再把精簡報告交給原 Codex session 檢查 workspace、review diff、驗證與下最後結論；需求理解、架構決策、高風險判斷與審核留給 Codex。`DIRECT` 問答仍只呼叫 Codex。若 Cursor CLI、認證、額度或執行失敗，relay 會以原始 prompt 回退到 Codex。

`DELEGATE` 任務會建立獨立的 job session，先回覆 job id，然後立即釋放主對話；背景 worker 完成後會自動回報。執行期間可傳送「進度」或使用 `/status` 查詢 job 狀態、執行秒數、最近一則進度與排隊數量，其他 DIRECT 訊息仍由主 session 處理。同一 thread 同時只執行一個背景 job；忙碌期間收到的新訊息不會丟棄，而是放入記憶體 FIFO queue，前一輪完成後自動執行並回覆。queue 預設最多 20 則，可用 `TGR_THREAD_QUEUE_MAX` 調整；服務重啟時尚未執行的記憶體 queue 不會保留。

Cursor relay 的主 session 會收到 Router／Orchestrator 指示；relay 建立的 `:job:` session 則明確標示為 Worker，直接執行與驗證，不得再次派工，避免巢狀 delegation。3600 秒是背景 Worker 的安全上限，不是主對話的等待時間。

```bash
cp deploy/router.toml.example router.toml
# 可選：指定其它位置
export TGR_ROUTER_CONFIG=/path/to/router.toml
```

Telegram 內可用：

```text
/router
/router test 請重構三個檔案、補 tests、跑完整測試並建立 PR
```

---

## 認證

### Cursor
- **OAuth（建議）**：在執行 bot 的同一使用者環境下執行 `agent login`，憑證存於家目錄。systemd 的 `User=` 必須與登入帳號一致。
- **API Key**：設定 `CURSOR_API_KEY` 環境變數。
- `/model` 會從 `agent --list-models` 動態取得最新清單並快取一小時；CLI 查詢失敗時才退回內建清單。

### Codex
執行 `codex login` 或依 CLI 文件設定 API Key。

### OpenCode
執行 `opencode` 並依互動式設定完成各 AI provider 的 API key 配置；或直接在環境變數設定對應 provider 的 key（如 `ANTHROPIC_API_KEY`、`OPENAI_API_KEY`）。

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
- `TGR_CODEX_BYPASS_APPROVALS_AND_SANDBOX=1` 會讓 Codex 跳過核准與 sandbox，僅適合已隔離的環境。
- `TGR_CLAUDE_SKIP_PERMISSIONS=1` 會讓 Claude CLI 跳過所有工具授權確認，僅適合已隔離的環境。
- Bot Token 與 API key 具有高風險寫入權限，請勿提交至版本控制。
