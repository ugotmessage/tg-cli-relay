# HMS → WEB1 背景完成回呼

預設關閉。這不是第二個 Telegram bot，也不是第二個 watchdog：WEB1 既有的單一 Relay process 在 marker 出現時凍結目的地；HMS 既有的 `cursor-worker-watchdog` 在任務進入 `done/` 或 `failed/` 之後，先送簽署登記 probe，再於 **HMS 任務 workspace** 跑一次全新 Codex 驗收，最後把凍結的驗收結果送回 WEB1，由同一個 Relay 投遞到原對話。WEB1 **不**跑 Codex。

## 為什麼沒有第二個 bot／watchdog

- 登記與 inbox 消費都掛在現有 `tg-cli-relay bot`（`post_init=_refresh_telegram_commands` 之後 `asyncio.create_task`，列入 `_BACKGROUND_TASKS`）。
- 忙碌判斷重用 `_ACTIVE_RELAY_THREADS`，不另建 registry。
- HMS 端只在現有 daemon 迴圈加一次有界 `enqueue`／`drain`（idle `sleep(10)` 前、每筆任務後 `sleep(2)` 前），不新增 watcher、不另開 poll loop。
- WEB1 **不**輪詢 HMS queue。

## WEB1

| 項目 | 路徑／值 |
|------|----------|
| feature flag | `TGR_BACKGROUND_HANDOFF=0`（unset／0／false／no／off 皆關閉） |
| state root | `/var/lib/tg-cli-relay/callback`（必須在 git repo 外，不可為 symlink） |
| secret file | `/etc/tg-cli-relay/callback.secret`（mode 0600） |
| receiver wrapper | `deploy/callback-receive.sh.example`（mode 0700；**預設關閉**，與 bot 相同 opt-in） |
| wrapper env | `/etc/tg-cli-relay/callback.env`（若存在則 source；在此明確設 `TGR_BACKGROUND_HANDOFF=1` 才啟用，不可放密鑰） |
| 狀態目錄 | `registrations/<bot_fp>/<task_id>.json`、`inbox/{new,claimed,completed,failed}/`、`seen/<event_id>`、`receipts/<task_id>.json` |

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
| HMS verifier argv | `HMS_CALLBACK_VERIFIER_ARGV`（JSON 字串陣列）或 `HMS_CALLBACK_VERIFIER_BIN`（預設 `/Users/ht/.hermes/scripts/tg-relay/codex-device-relay`） |
| HMS verifier timeout | `HMS_CALLBACK_VERIFIER_TIMEOUT`（預設 600 秒）；prompt 以 argv 最後一段或 `HMS_CALLBACK_VERIFIER_PROMPT=stdin` 傳入，禁止 shell 插值 |
| watcher drain timeout | `HMS_CALLBACK_DRAIN_TIMEOUT`（預設 900 秒，須大於 verifier timeout） |

建議 SSH 路由（只記錄路徑，不複製私鑰）：

- alias：`snote-internal-ssh`
- config：`/Users/ht/.hermes/secrets/ssh/config`
- remote command：WEB1 上的 `callback-receive` wrapper（stdin = 事件 JSON）

Watchdog patch 見 artifacts：`cursor-worker-watchdog.patch`。套用前備份 live `cursor-worker-watchdog.py`。enqueue 只在 queue 檔已進入 `done/` 或 `failed/` 且既有 `notify()`／log 之後；transport 失敗被吞掉，不改分類、不改通知文字、不 `replace()` 回 running。

## 無害 e2e fixture（deploy 後由 controller 跑）

1. 啟用 flag，確認 secret 與 state root。
2. 在測試對話送出一輪會印出整段 marker 的 Codex 回合：`TGR_BACKGROUND_HANDOFF:<task_id>:<abs_handoff>`。
3. 使用者應看到「已登記背景工作…」，看不到 marker 本體。
4. HMS queue 任務 `callback.enabled=true`，進入 `done/` 或 `failed/` 後 outbox 先送 signed `registration_probe`。
5. WEB1 回 `registered` 後，HMS 在任務 workspace 跑一次全新 `codex exec`（無 `resume`），原子寫入 verifier 輸出，再送 signed `final_result`。
6. WEB1 用凍結的 chat／topic 投遞已凍結的驗收文字，寫 `receipts/<task_id>.json`。WEB1 不跑 Codex。
7. 重送同一 final `event_id` 應為 replay、不重跑模型。壞簽章不得進 inbox。未知登記不得花費 verifier 回合。

本工作樹的單元測試不連網、不打真實 Telegram，不代表 live e2e 已通過。
