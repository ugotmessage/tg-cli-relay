# HMS → Relay 背景完成回呼

預設關閉。這不是第二個 Telegram bot，也不是第二個 watchdog：既有的單一 Relay process 在 **精確的 assistant protocol marker** 出現時，先核對 durable handoff 並凍結目的地，再結束送出該 marker 的主回合（不殺其他 process、不把拆離當成逾時）。HMS 既有的 `cursor-worker-watchdog` 在任務進入 `done/` / `failed/` / timeout 之後送簽署事件；Relay 用同一個 HMAC receiver 去重後，把 **一次** 續行排進原 Codex conversation／session。Inbox **不**再把獨立驗收文字直接送 Telegram，也 **不**另跑一個 verifier 模型。

## 為什麼沒有第二個 bot／watchdog

- 登記與 inbox 消費都掛在現有 `tg-cli-relay bot`（`post_init=_refresh_telegram_commands` 之後 `asyncio.create_task`，列入 `_BACKGROUND_TASKS`）。
- 忙碌判斷重用 `_ACTIVE_RELAY_THREADS`，不另建 registry。續行只在主 slot 空閒時執行；新的使用者訊息在 worker 仍 pending／failed／uncertain 時仍可進主對話。
- HMS 端只在現有 daemon 迴圈加一次有界 `enqueue`／`drain`（idle `sleep(10)` 前、每筆任務後 `sleep(2)` 前），不新增 watcher、不另開 poll loop。Drain **不再**跑獨立 Codex verifier。
- Relay **不**輪詢 HMS queue。

## WEB1

| 項目 | 路徑／值 |
|------|----------|
| feature flag | `TGR_BACKGROUND_HANDOFF=0`（unset／0／false／no／off 皆關閉） |
| state root | `/var/lib/tg-cli-relay/callback`（必須在 git repo 外，不可為 symlink） |
| secret file | `/etc/tg-cli-relay/callback.secret`（mode 0600） |
| receiver wrapper | `deploy/callback-receive.sh.example`（mode 0700；**預設關閉**，與 bot 相同 opt-in） |
| wrapper env | `/etc/tg-cli-relay/callback.env`（若存在則 source；在此明確設 `TGR_BACKGROUND_HANDOFF=1` 才啟用，不可放密鑰） |
| 狀態目錄 | `registrations/<bot_fp>/<task_id>.json`、`inbox/{new,claimed,completed,failed}/`、`seen/<event_id>`、`receipts/<task_id>.json` |
| 共用 queue 核對 | `TGR_BACKGROUND_HANDOFF_QUEUE_ROOT`（watchdog `queue/`；本機必填。未設定＝無法核對、拒絕拆離） |

### 密鑰

```bash
install -d -m 0700 /etc/tg-cli-relay /var/lib/tg-cli-relay/callback
openssl rand -out /etc/tg-cli-relay/callback.secret 32
# 或 64 hex：
# openssl rand -hex 32 > /etc/tg-cli-relay/callback.secret
chmod 0600 /etc/tg-cli-relay/callback.secret
chown root:root /etc/tg-cli-relay/callback.secret
```

同一份 32-byte 檔也要放到 HMS（例如 `/Users/ht/.hermes/secrets/callback.secret`，mode 0600）。密鑰只從檔案讀取，不可放進 argv、環境變數值、log 或 stdout。

### 服務

只對 **group2** 做序列 restart（Cursor 背景工作那一組），不要一次重啟兩組：

```bash
systemctl restart tg-cli-relay-group2.service
```

回滾：將 `TGR_BACKGROUND_HANDOFF` 設回 `0`（或 unset）後再 restart 同一個 unit。關閉後 marker 會當普通文字，不登記、不收 inbox、不投遞。

Receiver 由 HMS `ssh` stdin 呼叫 `tg-cli-relay callback-receive`（或 0700 wrapper）。wrapper 預設 `TGR_BACKGROUND_HANDOFF=0`，必須在 `/etc/tg-cli-relay/callback.env`（或環境）明確設為 `1` 才收事件。登記 probe 回 `registered`（0）或 `unknown_task`（2），且不寫 inbox、不送 Telegram、不呼叫模型。最終結果未知 task 結束碼 2、驗簽／schema 失敗 3、停用／設定錯誤 4。stdout 只有穩定 reason token。

## HMS

| 項目 | 路徑／值 |
|------|----------|
| outbox CLI | 本 repo `tools/hms_callback_outbox.py` |
| 部署後 watchdog 呼叫路徑 | `/Users/ht/.hermes/scripts/research/hms_callback_outbox.py`（deploy 時從 repo `tools/` 複製） |
| 覆蓋執行檔 | `HMS_CALLBACK_OUTBOX_BIN` |
| outbox 根目錄 | `HMS_CALLBACK_OUTBOX`（`pending/`、`sent/`、`failed/`；`unknown_task` 進 `failed/`，不進 `sent/`） |
| secret | `HMS_CALLBACK_SECRET_FILE` |
| SSH | `HMS_CALLBACK_SSH_CONFIG`、`HMS_CALLBACK_SSH_ALIAS`、`HMS_CALLBACK_SSH_REMOTE_CMD` |
| 單一本機 receiver | `HMS_CALLBACK_LOCAL_RECEIVER_ARGV`（JSON argv；與 ROUTES 互斥） |
| 本機 receiver allowlist | `HMS_CALLBACK_LOCAL_RECEIVER_ROUTES`（JSON object，最多 8 個 named argv） |
| watcher drain timeout | `HMS_CALLBACK_DRAIN_TIMEOUT`（預設 900 秒；drain 已不再跑模型，此值只覆蓋 transport 重試） |
| transport attempts | `HMS_CALLBACK_MAX_ATTEMPTS`（預設 60；每次指數退避，上限 60 秒） |

建議 SSH 路由（只記錄路徑，不複製私鑰）：

- alias：`snote-internal-ssh`
- config：`/Users/ht/.hermes/secrets/ssh/config`
- remote command：WEB1 上的 `callback-receive` wrapper（stdin = 事件 JSON）

若 Telegram Relay 與 HMS watcher 在同一台主機，設定
`HMS_CALLBACK_LOCAL_RECEIVER_ARGV` 為 JSON argv array，直接呼叫
`deploy/callback-receive-local.sh.example`；此時不經 SSH，登記 state 與 receiver
必須使用同一個 Relay env、bot token 與 state root。argv 只放 env／Python 路徑，
不得放 token 或 HMAC secret。未設定時仍使用上述 SSH transport。

同一台若有**兩個獨立 Codex Relay**（各自 bot token、DB、callback state root），
改設定 `HMS_CALLBACK_LOCAL_RECEIVER_ROUTES` 為 JSON object：route id → argv。
兩個變數不可同時設定。Probe 會詢問 allowlist 上每一個 destination，**必須剛好一個**
回 `registered` 才把該 route id 與 argv 寫進既有 outbox record；`final_result` 只送到
這個已選定的原始 receiver。零匹配依既有 backoff／attempt 上限重試或失敗；兩個以上
`registered`（同一 `task_id` 出現在兩個 bot）**fail-closed**（`ambiguous_route`），
不依 list 順序挑選、也不廣播。選定後的 transport 失敗只重試同一 route，不會改送另一個
bot。重試時 route id 必須仍在設定中且 argv **完全相等**，否則 `route_changed` 失敗。
無效或缺漏的 route 設定 fail-closed，錯誤只回穩定 token。既有單一 ARGV 與遠端單一 SSH
行為不變。Receiver 端既有 `bot_fingerprint` 核對仍有效。

本機兩個 receiver 的 argv 形狀（由 watcher wrapper 設定，不要寫進 task JSON）：

```text
/Users/ht/.hermes/scripts/tg-relay/callback-receive-codex.sh \
  <bot env> <venv python>
```

- `ht.tg-cli-relay`：`/Users/ht/.hermes/secrets/tg-relay-claude.env` + origin venv python
- `hms.codex-relay`：`/Users/ht/.hermes/codex-relay/.env` + `/Users/ht/.hermes/codex-relay/.venv/bin/python3`

## 雙 bot 操作 rollout（controller）

1. **備份**：兩個 Relay 的 session DB、env、callback state，以及 live
   `run-cursor-worker-watchdog.sh` 與 `hms_callback_outbox.py`。
2. **先不要清 secondary paused queue**（`#90 needs_review`、`#91-93 queued`；重複的
   paste-to-buy-toget-p4 通知與 `?`）。本修正不重跑、不刪那些列。
3. **Secondary 先設** `TGR_BACKGROUND_HANDOFF_QUEUE_ROOT`（與 primary 相同共用
   watchdog queue），再準備獨立 callback state root／secret file path（簽署密鑰可共用檔）。
4. **套用** outbox 與 watcher wrapper patch：`HMS_CALLBACK_LOCAL_RECEIVER_ROUTES`
   含兩個 named argv；`unset HMS_CALLBACK_LOCAL_RECEIVER_ARGV`。
5. **序列 restart**：一次只重一個 process（建議 shared watcher → 已通過的 primary
   保持 → 再 secondary）。確認原主 session mapping 仍在，不要 `/new`。
6. **Per-bot 無害 live 驗收**：各 bot 各做一次 probe→selected final→原 session 續行；
   確認 A 的 task 不會進 B 的 inbox／Telegram，反之亦然。
7. 已知 **done/failed-task-before-marker** 競態：watchdog 若在 Relay 登記 marker 前就
   enqueue，probe 會 `unknown_task` 並依既有政策失敗。不要為了這點另做信任通道或重跑
   不確定的 paused 任務。

Watchdog patch 見 artifacts：`/Users/ht/.hermes/artifacts/relay-background-continuation-20261028/cursor-worker-watchdog.patch`。套用前備份 live `cursor-worker-watchdog.py`。enqueue 只在 queue 檔已進入 `done/` 或 `failed/` 且既有 `notify()`／log 之後；transport 失敗被吞掉，不改分類、不改通知文字、不 `replace()` 回 running。timeout 分類為 `timed_out`，仍走同一條續行事件，不另開 queue。

## 無害 e2e fixture（deploy 後由 controller 跑）

1. 啟用 flag，確認 secret 與 state root。
2. 在測試對話送出一輪會印出整段 marker 的 Codex 回合：`TGR_BACKGROUND_HANDOFF:<task_id>:<abs_handoff>`。handoff 檔與共用 queue 的 `pending/`／`running/` 任務 JSON（`callback.enabled` + 相符 `handoff_path`）必須已存在；Relay 必須看得到 `TGR_BACKGROUND_HANDOFF_QUEUE_ROOT`。
3. 使用者應看到「已登記背景工作…」，看不到 marker 本體；該主回合應已釋放，後續使用者訊息可在 worker 仍 running 時完成。
4. 共用 queue 任務 `callback.enabled=true`，進入 `done/` / `failed/` / timeout 後 outbox 先送 signed `registration_probe`。
5. Receiver 回 `registered` 後，outbox **不**跑獨立 verifier，直接送 signed `final_result`。
6. Relay 在原 Codex session 空閒時續行一次，範圍僅限該 task／handoff／artifacts；用凍結的 chat／topic 投遞 Codex 回覆，寫 `receipts/<task_id>.json`（`continuation_queued`）。
7. 重送同一 final `event_id` 應為 replay、不排第二次續行。壞簽章不得進 inbox。未知／錯 bot 登記不得排程。in-flight 續行若結果不明，不得自動重跑，也不得暫停後續普通主對話。

## 凍結 session、`/new`、續行 UI

拆離時會把 **Codex `thread.started` / app-server thread id** 寫進 registration 的 `codex_session_id`（CLI 在 `store.upsert` 之前凍結）。續行用這個 id 做 `codex exec resume`，**不會**覆寫目前主對話 SessionStore。因此：

- `/new`、`/reset` 仍可在主 slot 空閒時使用（不會為了背景工作而禁止）。
- 之後的使用者訊息走新的主 session；背景續行仍回到**原** Codex session。
- Telegram 投遞仍用凍結的 chat／topic，所以同一對話看得到續行文字，但 Codex 歷史已分叉。
- 續行不會 `bind_parent_message`，回覆續行訊息不會把主 UI 綁到舊 session。
- Relay 重啟後，durable queue 的 `event_id` + `resume_session_id` 仍指向原 session。
- 舊登記若沒有 `codex_session_id`：**可見失敗**，絕不默默改用目前／新 session。

## 事件去重

續行以完整 callback `event_id`（UUID 字串）存在 `relay_pending_turns.event_id` 做 UNIQUE。Telegram 使用者訊息仍用 `(thread_key, message_id, kind)`。不再用 UUID 截斷當 identity。

## App Server 拆離

必須 `turn/interrupt` RPC 成功，且隨後收到 `turn/completed`，才把 `handoff_detached=True`。interrupt 失敗則繼續該回合，不釋放父對話為成功 handoff。CLI SIGTERM／resume 仍需 live Codex 驗收，假 executable 不能證明真實 CLI 行為。

## Durable task 核對

`accept_handoff_marker` 除了 handoff 檔存在，還要在 `TGR_BACKGROUND_HANDOFF_QUEUE_ROOT/{pending,running}/<task_id>.json` 找到 `id` 相符、`callback.enabled`、且 `callback.handoff_path` 與 marker 相同的任務。缺 queue root（遠端 WEB1 收件端看不到本機 HMS queue、且既有 SSH 只在完成後送 probe）時 **fail-closed**（`unverified_task`），不發明新的信任通道。

本工作樹的單元測試不連網、不打真實 Telegram，不代表 live e2e 已通過。
