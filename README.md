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
    ├── Reply / Task ID → 明確指定既有工作，普通訊息不做語意分類
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

**各 CLI 自行維護完整的 Session 歷史及 Agent 派工策略**。Relay 儲存 Session ID、Task 對應與有界的工作交接摘要；不按訊息內容猜測是否建立背景工作。

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
| `/force <指令>` | 全部 | 執行中立即修正：Codex App Server 優先即時 steer；其他 CLI 中止原回合並優先續送修正指令 |
| `/stop` | 全部 | 要求中止目前主對話執行，保留原本待處理訊息，不會自動重跑被中止的工作 |
| `/status` | 全部 | 顯示目前 provider、工作目錄、session 狀態 |
| `/provider` | 全部 | 列出可切換的 CLI provider |
| `/provider <id>` | 全部 | 切換 provider（例如 `claude`、`codex`） |
| `/model` | 全部 | 依 provider 列出常用模型清單 |
| `/model <id>` | 全部 | 切換目前 provider 的模型（下一輪起生效） |
| `/model <provider> <id>` | 全部 | 切換 provider 並設定該 provider 的模型 |
| `/task` | 全部 | 列出本對話的孤立任務 |
| `/task <id>` | 全部 | 顯示該任務狀態（此回覆會綁定該任務，回覆它可以繼續） |
| `/task <id> <說明>` | 全部 | 把說明送到該任務的獨立 session |
| `/task new <說明>` | 全部 | 開新的孤立任務（含小型 DIRECT） |
| `/help` | 全部 | 顯示可用指令 |

> 每個 Telegram thread 會保存自己的 provider。模型偏好依 provider 分開保存（`model:cursor`、`model:claude` 等）。**Cursor relay 預設一律 `--model auto`**，除非你透過 `/model` 或 `TGR_CURSOR_MODEL` 明確指定；不會沿用舊版通用 `model` 偏好或其它程式的 CLI 指定。

### 任務隔離（task isolation）

只有**明確**的 `/task new` 會建立 `{parent}:job:{task_id}` 獨立 session，來源／ACK／進度／狀態／結果都綁在該任務上。Reply 至已綁定任務訊息會繼續**同一個**任務。細節見 `docs/task-isolation.md`。

- 普通訊息不論是「繼續」、「進度」或新開發要求，**全部交給主 Agent CLI**；Relay 不會根據關鍵字或最近 Task 焦點更換 Session。
- Reply 至主對話訊息（或未綁定的舊訊息）會附上 Telegram 引用訊息的 ID 與可用文字，交給主 Agent；無引用文字時只提供 ID，不猜測內容。
- 想直接操作 Relay 建立的 Task，請 Reply 帶 `[task ...]` 的訊息，或使用 `/task <id> <說明>`。Agent CLI 自己啟動的 Cursor／Subagent 不屬於 Relay Task，仍交由主 Agent 管理。

---

## 統一 10 分鐘超時與自動恢復

- `TGR_TURN_TIMEOUT=600`（預設）統一限制由 Relay 發起的每個 CLI 回合，包括 Codex CLI、Codex App Server、Cursor、Claude、OpenCode，以及使用者明確要求的 Relay Task。Agent 自己啟動的獨立背景 Worker 不由 Relay 控制；這不是自動建立新的 Worker。
- **正常主對話超時**：保留原始 Telegram 訊息 ID 和有界原文（最多 12,000 字元），在主對話等待佇列最前面插入一次系統恢復提示。由原本的主 Agent 根據自己的知識庫、既有 Session 與工作目錄檢查進度，決定接續或派工。Relay 不指定 Cursor 或其他 Worker。
- **恢復指令再次超時**：停止自動恢復這筆工作，下一則等待中的訊息照常送進主 CLI；避免無限重試。使用者手動 `/stop` 或 `/force` 的中斷不會觸發自動恢復。
- **顯式 Task**：`/task new` 產生的獨立 Task 若超時會回報失敗，不會再由 Relay 自動分派新的工作。
- **佇列保護**：顯式背景 Task 等待同時另一個 Task 結束時，不阻擋後面的普通主對話。等待佇列會先寫入同一份 SQLite Session DB 再 ACK。Telegram Bot 重啟時也不會丟棄伺服器尚未取回的更新。重啟時僅自動補送 **尚未開始執行** 的 `queued` 訊息；若偵測到上一輪已在執行卻無法確認結果，會暫停該 chat/topic 的佇列，以免重複修改程式、資料庫或重複交易。
- **Session 注意事項**：Codex App Server、既有 Cursor Session 等可接續先前歷史；Claude／OpenCode 首輪若還沒輸出 Session ID 就遭到中斷，原生 Session 恢復不一定可行。Relay 的恢復提示仍會提供原始工作與可用的上下文，但不是保證原生 Session 100% 完整還原。

## 持久化等待佇列（SQLite）

Telegram 的主對話不會驗證 Agent 是否真的將工作派出去：它只會在**超時恢復指令本身的 CLI 回合正常結束／逾時結束**後，再執行下一則等待訊息。Agent 是否派 Cursor 或其他 Worker 仍由 Agent 知識庫決定，Relay 不監督派工成敗。

Relay 將原始 Telegram message ID、chat/topic、Provider、佇列類型、已解析的 Prompt、Task ID 和執行狀態保存在既有 `TGR_SESSION_DB` 下的 `relay_pending_turns` 資料表：

| SQLite 狀態 | 意義 | 服務重啟後 |
|---|---|---|
| `queued` | 已收到、尚未開始跑 | 若同對話沒有不明執行，就依 FIFO／恢復優先順序**自動續送** |
| `running` | CLI 已開始執行 | 重啟時改為 `needs_review`，**絕不自動重跑** |
| `needs_review` | 先前程序中斷，結果不明 | 暫停同一 chat/topic 其餘等待訊息，供人工查詢 |
| `done` / `cancelled` / `reviewed` | 已處理、取消或人工確認 | 不自動續送 |

- `/queue`：顯示這個 Telegram chat/topic 的等待與結果不明項目。
- `/queue resume`：使用者自行確認已檢查中斷工作後，將 `needs_review` 標為已確認，**不重跑**它們，只恢復後續 `queued` 訊息。恢復本身仍會尊重主對話與 Task Session 的執行序。
- 如果上一輪是「超時後的自動恢復指令」，則等其 CLI 回合結束才會釋放後續 FIFO；CLI 回傳成功**不代表 Relay 已證實 Worker 成功啟動或完成**。
- Telegram Bot 啟動時才檢查舊的 `running` 轉為結果不明；服務正常運作時的訊息不需要 `/queue resume`。
- 不提供跨程序的 exactly-once 保證：Crash 發生於 Telegram 收到更新但 SQLite 尚未完成寫入等非常小的時間窗，仍可能需要由 Telegram 重新推送，或由使用者確認。
- **安全性**：SQLite 會保存排隊的訊息文字與 Prompt，請限制資料庫檔案／備份的存取權限；已下載的附件檔案若過期或遺失，恢復前需人工確認。

## 即時修正與停止

- `/force <修正內容>`：只有主對話正在執行時可用。若 Codex 使用 `TGR_CODEX_TRANSPORT=app-server`，Relay 優先將內容送入原本執行中的 Turn。Cursor、Claude、OpenCode、Codex CLI 不支援相同的即時輸入介面，因此改為**中止目前回合**並將修正內容插在主對話 FIFO 佇列最前方，以原 Session 接續；未完成的操作及檔案不會自動回復。
- `/stop`：要求停止目前的**主對話執行**，不清空後面的佇列，也不重新執行已停止的工作。獨立於 Relay 的 Agent 自行管理的背景 Worker 不在控制範圍內。
- 此功能不替 Agent 判斷派工方式，也不替代未來的超時恢復機制。注意這裡的 CLI 停止是依程序／Codex App Server 中斷請求，Provider 自己啟動且完全脫離控制的背景進程可能繼續運作。
- 每次 Telegram Bot 正常啟動時，會透過 `set_my_commands` **重新註冊完整的指令選單**；若 Telegram API 當下不可用，會記錄錯誤，但不阻止 Bot 提供訊息服務。

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
| `TGR_TURN_TIMEOUT` | `600` | 所有由 Relay 啟動的 Codex CLI／App Server、Cursor、Claude、OpenCode 回合統一超時秒數；`0` 表示不限制 |
| `TGR_THREAD_QUEUE_MAX` | `20` | 每個 Telegram thread 忙碌時可緩存的訊息上限；FIFO 自動接續處理 |
| `TGR_CODEX_BIN` | `codex` | OpenAI Codex CLI 路徑 |
| `TGR_CODEX_BYPASS_APPROVALS_AND_SANDBOX` | （未設定） | 設為 `1` 啟用 `--dangerously-bypass-approvals-and-sandbox` |
| `TGR_CODEX_TRANSPORT` | `cli` | 預設沿用 `codex exec`；設 `app-server` 啟用持續連線、原生 Session 與執行中 Reply steering |
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

## 明確路由與 Agent 派工

- **普通訊息**：直接送至 Telegram chat/topic 對應的主 CLI Session，保留該 Provider 的完整歷史。Relay 不執行 Task Router、分類器、模糊追問判斷或 Codex→Cursor 強制派工。
- **Reply 至主對話訊息**：使用 Telegram 的 `reply_to_message` 內文提供有界引用文字（最長 4,000 字元）、原訊息 ID 與這次使用者要求。被引用的內容只作參考，不是另一條指令。沒有內文時標示為未知；不會因此進入其他 Task。
- **Reply 至 Relay Task 訊息**：依已記錄的 Telegram message→Task 綁定，直接轉給對應 Task Session（限定同一 chat/topic）。
- **明確 `/task new`**：使用者自行建立獨立 Task；CLI 的原生 Worker／Cursor 派工仍由它自己的知識庫與規則管理，Relay 不介入。
- **等待佇列**：忙碌期間的普通訊息按順序處理（SQLite 持久化＋持久 FIFO，預設最多 20 則）；重啟會載入尚未啟動的訊息，遇到結果不明的工作則暫停，等待明確 `/queue resume`。所有 Relay 管理的回合預設 10 分鐘超時。主對話超時時，Relay 會優先插入**一輪**同 Session 的恢復指令，再繼續普通訊息 FIFO；恢復指令再超時不會無限重試。

舊的 `task_router.py`、`worker_selector.py` 及其設定是遷移相容的歷史程式碼，不再參與 Telegram 普通訊息的預設執行路徑。

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
