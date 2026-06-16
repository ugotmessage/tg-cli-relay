# TG Relay 收編 + Telegram 韌性 — 實作交接文件

> **狀態**：Phase A / B / D 完成；Cursor 已收編至統一 codebase 並由 LaunchAgent 常駐（C1/C2 尚未開始）<br>
> **建立**：2026-06-16<br>
> **目標主機範例**：`ht` 本機（三條 relay 並行）<br>
> **程式 SSOT（目標）**：`~/projects/tg-cli-relay-claude`<br>
> **不依賴**：`~/.hermes/hermes-agent`（Gateway）；網路層必須自包含在 relay repo 內

---

## 1. 背景與問題

### 現象

- Claude TG relay 間歇「不理人」：log 有 `TimedOut` / `502 Bad Gateway` / `httpx.ReadError`
- `launchctl` 顯示 relay 反覆重啟（`KeepAlive` + `ThrottleInterval` 放大抖動）
- 與 HMS Gateway **無關**（不同 bot token）；與 `notify.py` **無關**

### 根因（兩層）

| 層 | 說明 | 可控 |
|----|------|------|
| L1 | Telegram API 對特定 bot 間歇 timeout / 502 | 僅能繞路（fallback IP）或換 token |
| L2 | relay 對 Telegram 錯誤不夠耐打 → polling/typing 中斷 → crash loop | **可根治（程式）** |
| L3 | Claude `--resume` 子行程掛住（長 session） | 緩解（timeout、`/new`） |

### 已做（接手前請核對）

- [x] `telegram_bot.py`：`sendChatAction` 失敗只 log warning，不中斷 handler
- [x] `run_polling(..., bootstrap_retries=-1)`
- [x] `claude_cli.py`：過濾 `[Background process proc_…]` 與 JSON 中繼資料洩漏到 TG

> 若以上已存在，勾選後繼續 Phase A。

---

## 2. 目標與非目標

### 目標

1. **一套程式** `tg-cli-relay-claude` 跑三條 LaunchAgent（Claude / Cursor / Codex）
2. 每條實例仍可有：**獨立 bot token、工作目錄、backend/LLM、session DB**
3. **自包含** Telegram 網路韌性（fallback IP、timeout、重試），不 import HMS Gateway
4. 無 HMS 的主機也能 `pip install -e '.[telegram]'` 跑起來

### 非目標

- 不把 relay 升級成 Hermes Gateway
- 不共用 bot token（仍三 bot 三 process）
- 不在此文件處理 Gateway 的 `background_process_notifications`

---

## 3. 現況盤點（2026-06-16）

| Relay | LaunchAgent | 程式目錄 | venv import 來源 | env SSOT |
|-------|-------------|----------|------------------|----------|
| Claude | `ht.tg-cli-relay` | `projects/tg-cli-relay-claude` | `tg-cli-relay-claude/src` | `~/.hermes/secrets/tg-relay-claude.env` |
| Cursor | `ht.tg-cli-relay-cursor` | `projects/tg-cli-relay-claude` | `tg-cli-relay-claude/src` | `~/.hermes/secrets/tg-relay-cursor.env` |
| Codex | `hms.codex-relay` | `~/.hermes/codex-relay` | `tg-cli-relay-claude/src` (editable) | `~/.hermes/codex-relay/.env` |

### 驗證指令（接手第一步必跑）

```bash
# 三條是否活著
launchctl list | rg 'ht.tg-cli-relay|hms.codex-relay'

# 各 venv 實際 import 哪份 source
/Users/ht/projects/tg-cli-relay-claude/.venv/bin/python3 -c "import tg_cli_relay; print(tg_cli_relay.__file__)"
/Users/ht/projects/tg-cli-relay-claude/.venv/bin/python3 -c "import tg_cli_relay; print(tg_cli_relay.__file__)"
/Users/ht/.hermes/codex-relay/.venv/bin/python3 -c "import tg_cli_relay; print(tg_cli_relay.__file__)"

# Telegram getMe 延遲（不印 token）
python3 - <<'PY'
import time, urllib.request
from pathlib import Path
def token_from(path):
    for line in Path(path).read_text().splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=",1)[1].strip().strip('"')
    return ""
for name, p in [
    ("claude", Path.home()/".hermes/secrets/tg-relay-claude.env"),
    ("cursor", Path.home()/ ".hermes/secrets/tg-relay-cursor.env"),
    ("codex", Path.home()/".hermes/codex-relay/.env"),
]:
    t = token_from(p)
    if not t: continue
    t0=time.time()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{t}/getMe", timeout=15)
        print(name, "getMe OK", round(time.time()-t0,2), "s")
    except Exception as e:
        print(name, "getMe FAIL", round(time.time()-t0,2), "s", e)
PY
```

**Check 通過標準**：三條 launchctl 有 PID；claude/cursor/codex import 指向 `-claude/src`；getMe 多數 < 3s（間歇失敗可記錄）。

---

## 4. 目標架構

```
~/projects/tg-cli-relay-claude/          # 唯一 codebase
  src/tg_cli_relay/
    telegram_network.py                  # 新增：fallback IP transport（自包含）
    telegram_app.py                      # 新增：build_resilient_application()
    telegram_bot.py                      # 改：用 telegram_app
    ... (既有 relay / providers)

LaunchAgent × 3（各載不同 .env，互不搶 token）：
  ht.tg-cli-relay          → wrapper → claude env
  ht.tg-cli-relay-cursor   → wrapper → cursor env   # 收編後改走 claude venv
  hms.codex-relay          → run.sh  → codex env    # 已是 claude source
```

### 每實例 env 範例（收編後仍分開）

| 變數 | Claude | Cursor | Codex |
|------|--------|--------|-------|
| `TELEGRAM_BOT_TOKEN` | claude bot | cursor bot | codex bot |
| `TGR_BACKEND` | `claude` | `cursor` | `codex` |
| `TGR_ENABLED_BACKENDS` | `claude`（建議鎖死） | `cursor` | `codex` |
| `TGR_DEFAULT_WORKSPACE` | `/Users/ht` | `/Users/ht/` | `/Users/ht/.hermes` |
| `TGR_SESSION_DB` | `.../tg-cli-relay-claude/data/sessions.sqlite3` | `.../tg-cli-relay-cursor/data/sessions.sqlite3`（新建） | `~/.hermes/codex-relay/data/sessions.sqlite3` |

模型：實例層用 `TGR_BACKEND`；對話層用 TG `/model`（存在各自 sqlite）。可選加 `TGR_DEFAULT_MODEL`（見 Phase C）。

---

## 5. 實作階段（Todo + Check）

> **慣例**：每完成一 Phase，更新文件開頭「狀態」、勾選 checkbox、commit（若使用者要求）。

---

### Phase A — Telegram 網路韌性（claude repo 內）

**目的**：L2 根治；不依賴 HMS。

#### A1. 新增 `telegram_network.py`

- [x] 從 `~/.hermes/hermes-agent/gateway/platforms/telegram_network.py` **抄寫精簡版**（勿 runtime import Hermes）
- [x] 保留：`TelegramFallbackTransport`、DoH / seed IP、`rewrite_request_for_ip`
- [x] 支援 env：`TGR_TELEGRAM_FALLBACK_IPS`（逗號分隔，例 `149.154.166.110,149.154.167.220`）
- [x] 支援 env：`TGR_TELEGRAM_PROXY`（可選，與 Gateway 行為對齊則更好）

**Check A1**

```bash
cd ~/projects/tg-cli-relay-claude
.venv/bin/python -c "from tg_cli_relay.telegram_network import discover_fallback_ips; print(discover_fallback_ips())"
# 預期：至少印出一個 IPv4 或空 list（不拋例外）
```

#### A2. 新增 `telegram_app.py`

- [x] `build_resilient_application(token: str) -> Application`
- [x] 使用 `HTTPXRequest` 自訂 `connect_timeout` / `read_timeout`（建議 30s / 60s，可 env 覆寫）
- [x] `get_updates_request` 與一般 request 共用 fallback transport
- [x] `ApplicationBuilder().token(token).request(...).get_updates_request(...).build()`

**Check A2**

```bash
.venv/bin/python -c "
from tg_cli_relay.telegram_app import build_resilient_application
import os
from pathlib import Path
# 從 claude env 讀 token 測 build（不啟動 polling）
for line in Path.home().joinpath('.hermes/secrets/tg-relay-claude.env').read_text().splitlines():
    if line.startswith('TELEGRAM_BOT_TOKEN='):
        os.environ['TELEGRAM_BOT_TOKEN']=line.split('=',1)[1].strip()
        break
app = build_resilient_application(os.environ['TELEGRAM_BOT_TOKEN'])
print('app ok', type(app))
"
```

#### A3. 改 `telegram_bot.py`

- [x] `run_bot()` 改用 `build_resilient_application(token)`
- [x] 保留 `bootstrap_retries=-1`、`drop_pending_updates=True`（claude 既有）
- [x] `_typing_loop` 已 try/except — 確認仍在
- [x] `reply_text` 可選：502/timeout 時重試 2 次（間隔 2s）

**Check A3**

```bash
.venv/bin/pytest tests/ -q
launchctl kickstart -k "gui/$(id -u)/ht.tg-cli-relay"
sleep 5
tail -5 ~/.hermes/logs/tg-relay-claude/stderr.log
# 預期：啟動 Telegram bot；getUpdates 200
```

#### A4. 測試

- [x] 新增 `tests/test_telegram_network.py`（mock transport 或純函式測試）
- [x] 新增 `tests/test_telegram_app.py`（build 不崩）

**Check A4**：`pytest tests/ -q` 全綠。

---

### Phase B — Cursor 收編到同一 codebase

**目的**：三條 relay 只維護 `tg-cli-relay-claude`；Cursor 不再依賴 `projects/tg-cli-relay`。

#### B1. 準備 Cursor 專用 env

- [x] 建立 `~/.hermes/secrets/tg-relay-cursor.env`（chmod 600），內容自 `projects/tg-cli-relay/.env` 遷移
- [x] 設定：
  - `TGR_BACKEND=cursor`
  - `TGR_ENABLED_BACKENDS=cursor`
  - `TGR_DEFAULT_WORKSPACE=/Users/ht/`（或你指定的專案根）
  - `TGR_SESSION_DB=/Users/ht/projects/tg-cli-relay-claude/data/sessions-cursor.sqlite3`（**與 claude 分開**）
  - `TGR_CURSOR_AGENT_BIN=...`
  - `TELEGRAM_BOT_TOKEN=<cursor bot>`
  - `TGR_ALLOWED_TELEGRAM_USER_IDS=...`

**Check B1**：`grep TGR_BACKEND ~/.hermes/secrets/tg-relay-cursor.env` → `cursor`

#### B2. 新增 wrapper script

- [x] 建立 `~/.hermes/scripts/tg-relay/run-cursor-relay.sh`（仿 `run-claude-relay.sh`）
- [x] `source ~/.hermes/secrets/tg-relay-cursor.env`
- [x] `exec .../tg-cli-relay-claude/.venv/bin/python3 -m tg_cli_relay bot`

**Check B2**：手動跑 5 秒無 crash

```bash
timeout 5 ~/.hermes/scripts/tg-relay/run-cursor-relay.sh || true
# 預期：看到「啟動 Telegram bot」；timeout 結束正常
```

#### B3. 改 LaunchAgent plist

- [x] 編輯 `~/Library/LaunchAgents/ht.tg-cli-relay-cursor.plist`
- [x] `ProgramArguments` → `run-cursor-relay.sh`
- [x] `WorkingDirectory` → `~/projects/tg-cli-relay-claude`
- [x] Log 建議改到 `~/.hermes/logs/tg-relay-cursor/`（與 claude 分 log）

**Check B3**

```bash
launchctl bootout "gui/$(id -u)/ht.tg-cli-relay-cursor" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/ht.tg-cli-relay-cursor.plist
launchctl kickstart -k "gui/$(id -u)/ht.tg-cli-relay-cursor"
pgrep -fl 'tg_cli_relay bot'  # 應有 cursor 對應 process
lsof -p <cursor_pid> | awk '/cwd/{print}'  # 應為 tg-cli-relay-claude
```

#### B4. 更新文件

- [x] 更新 `~/.hermes/knowledge/hms-tg-relay-overview.md`（Cursor 列改為 claude repo + secrets path）
- [x] `projects/tg-cli-relay` 標註 **runtime 已退役，僅留檔案備查**（README 一行即可，非必須）

**Check B4**：`ht.tg-cli-relay-cursor` 的 python cwd 為 `-claude`，且 TG cursor bot 能回覆。

---

### Phase C — 可選增強

#### C1. 預設模型 env

- [ ] 支援 `TGR_DEFAULT_MODEL` 或 `TGR_DEFAULT_MODEL_<backend>`（啟動時寫入 thread pref 僅當尚未設定）
- [ ] 文件寫入 `env.example`

**Check**：新 thread 第一則訊息 footer 顯示指定 model。

#### C2. `tg_cli_relay doctor` 子命令

- [ ] 檢查：env 必填、`getMe` 延遲、fallback IP、session DB 可寫、是否孤兒雙實例

```bash
.venv/bin/python -m tg_cli_relay doctor
# 預期：exit 0，列出三項以上 OK
```

#### C3. 降低 crash loop 放大（謹慎）

- [ ] 評估 `ThrottleInterval` 30→120（三份 plist）
- [ ] 或 `KeepAlive` 改為僅異常退出才重啟（需測 launchd 行為）

**Check**：人為 kill -9 後只重啟一次，log 無 30s 連續啟動十几次。

#### C4. Claude 子行程逾時

- [ ] `tg-relay-claude.env` 設 `TGR_CLAUDE_TIMEOUT=600`（10 分鐘，依使用者偏好）
- [ ] 文件註明卡住時 `/new`

---

### Phase D — Codex 對齊（通常 Phase A 後已生效）

Codex 已 editable install `tg-cli-relay-claude`，Phase A 完成後：

- [x] 確認 `~/.hermes/codex-relay/.venv` 仍指向 claude src：`pip install -e ~/projects/tg-cli-relay-claude`
- [x] 重啟 `hms.codex-relay`
- [ ] 可選：codex `run.sh` 改呼叫統一 wrapper

**Check D**

```bash
launchctl kickstart -k "gui/$(id -u)/hms.codex-relay"
# TG codex bot 發「ping」有回覆
```

---

## 6. 整體驗收（上線前必做）

```bash
# 1) 三條都在
launchctl list | rg 'ht.tg-cli-relay|hms.codex-relay'

# 2) 三顆 bot getMe
# （用第 3 節腳本）

# 3) 各發一則 TG 測試訊息，30s 內有回覆
#    - claude_clawbot
#    - cursor_clawbot
#    - codex_clawbot

# 4) 今日 timeout 計數不持續飆升
rg -c 'TimedOut|Bad Gateway|502' ~/.hermes/logs/tg-relay-claude/stderr.log
# 觀察 1 小時內是否仍每分鐘增加

# 5) 無 409 Conflict（雙實例搶 token）
rg '409|Conflict' ~/.hermes/logs/tg-relay-*/stderr.log
```

| 項目 | 通過標準 |
|------|----------|
| 三 bot 回覆 | 各 1 則測試訊息 < 60s 回覆（claude 長任務除外） |
| getMe | 連續 5 次皆 < 5s |
| crash loop | 1 小時內無「每 30s 啟動一次」pattern |
| 工作目錄 | `/status` 顯示各 bot 對應 workspace |
| backend | `/status` 顯示 claude / cursor / codex 各正確 |

---

## 7. 回滾

### Phase A 回滾

```bash
git checkout -- src/tg_cli_relay/telegram_bot.py
rm -f src/tg_cli_relay/telegram_network.py src/tg_cli_relay/telegram_app.py
launchctl kickstart -k "gui/$(id -u)/ht.tg-cli-relay"
```

### Phase B 回滾（Cursor）

```bash
# 還原 plist 指向 projects/tg-cli-relay/.venv
launchctl bootout "gui/$(id -u)/ht.tg-cli-relay-cursor"
# 載入舊 plist 備份
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/ht.tg-cli-relay-cursor.plist.bak
```

> **接手者**：改 plist 前先 `cp plist plist.bak.$(date +%Y%m%d)`

---

## 8. 檔案清單（預計變更）

| 路徑 | 動作 |
|------|------|
| `projects/tg-cli-relay-claude/src/tg_cli_relay/telegram_network.py` | 新增 |
| `projects/tg-cli-relay-claude/src/tg_cli_relay/telegram_app.py` | 新增 |
| `projects/tg-cli-relay-claude/src/tg_cli_relay/telegram_bot.py` | 修改 |
| `projects/tg-cli-relay-claude/tests/test_telegram_*.py` | 新增 |
| `projects/tg-cli-relay-claude/env.example` | 補 fallback IP 說明 |
| `~/.hermes/scripts/tg-relay/run-cursor-relay.sh` | 新增 |
| `~/.hermes/secrets/tg-relay-cursor.env` | 新增 |
| `~/Library/LaunchAgents/ht.tg-cli-relay-cursor.plist` | 修改 |
| `~/.hermes/knowledge/hms-tg-relay-overview.md` | 更新對照表 |

**勿改**（除非使用者明確要求）：`~/.hermes/hermes-agent/`、Gateway plist、`notify.py` 路由。

---

## 9. 接手備註

- **Claude session 卡住**（log 只有 `sendChatAction`）：`pgrep -fl 'claude --print'` → kill 該 PID；或 TG `/new`
- **與 HMS Gateway 分界**：Gateway bot ≈ `8619982395`；relay 三 bot 各不同（見第 3 節 env）
- **無 HMS 主機部署**：只需 `tg-cli-relay-claude` + `.env` + `python -m tg_cli_relay bot`
- **進度回填**：完成 Phase 後在本文件第 1 節「狀態」寫明，例如：`Phase A 完成，Phase B 進行中`

---

## 10. 進度紀錄（接手者填寫）

| 日期 | 負責 | Phase | 結果 |
|------|------|-------|------|
| 2026-06-16 | — | 文件建立 | 待實作 |
| 2026-06-16 | Codex 子代理 | Phase A + Phase B | 完成（pytest 全綠、LaunchAgent 已切至統一 codebase） |
| 2026-06-16 | Hermes Agent | Phase B/D 驗收 | 修正 per-token lock 避免多 bot 同 data dir 互鎖；Cursor/Claude/Codex LaunchAgent 皆 running；pytest 13 passed |
