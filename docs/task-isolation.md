# Telegram 任務隔離（task isolation）

每個背景任務使用獨立 CLI session（`{parent}:job:{task_id}`），並把 Telegram 訊息綁到該任務。回覆任務相關訊息會繼續**同一個**任務 session，不會因為另一個任務正在跑、或 bot 重啟，就掉回混在一起的父對話。

資料存在該 bot 既有的 SQLite（`TGR_SESSION_DB`）裡，只新增 `tasks` / `task_messages` 兩張表，不改 `sessions` / `preferences`。

## 怎麼選任務（確定性，沒有 LLM 分類）

| 輸入 | 行為 |
|---|---|
| 尚未有任何任務的普通文字 | 與以前相同：DIRECT 走父 session；DELEGATE 開新孤立任務 |
| 新的 DELEGATE 訊息 | **一定**開新任務與新 session，不重用其它任務或父 session |
| `/task new <說明>` | 同樣開新孤立任務；小型 DIRECT 題目也可以，不會灌入舊的父對話歷史 |
| 回覆已綁定的來源／ACK／進度／結果／附件／錯誤 | 送到**該則訊息所屬任務**的 session |
| `/task <id>` | 顯示該任務狀態 |
| `/task <id> <說明>` | 把說明送到該任務 session |
| `/task` | 列出本對話最近任務 |
| 未綁定的回覆，且本對話已有任務 | **不**呼叫 provider；請回覆 `[task …]` 訊息或 `/task <id>` |
| 未串接的「進度／做到哪」且有多個任務 | **不**呼叫 provider；列出任務與選法 |
| 未串接的進度查詢且只有一個明確任務 | 只回狀態，不重跑 |
| 無關的新問題（不是回覆、不是 `/task`、不是進度查詢） | 不當成延續。DIRECT 走父對話；DELEGATE 開新任務 |
| 未知、他對話／他主題、或不存在的 task id | 明確失敗，**絕不**默默落到父對話 |

跨 chat、跨 forum topic、跨 bot DB 的訊息與任務互不相通。任務執行時使用**建立時**記下的 backend 與該任務的 worker key；父對話當下的 `/provider` 不會把別人的 session 接過來。建立任務時會把父 thread 的 `model:{backend}` 偏好**複製**到 worker key，之後互不污染。

## 排隊

- 父對話的新 DELEGATE / `/task new` 仍維持原本的順序與忙碌排隊，主對話 DIRECT 在背景任務執行期間仍可回應。
- 對**同一任務**的 follow-up 在訊息到達當下就凍住 `task_id` / `worker_key` / `backend`，不會等到 dequeue 才重判。
- 同一任務 session 同一時間只會寫入一輪；後到的 follow-up 等上一輪結束。
- 不同任務的 session 可以並行（例如回覆已完成的 A，同時 B 還在跑）。

## 顯示與生命週期

所有任務的 ACK、進度、狀態、最終分段、附件失敗、錯誤都會帶 `[task <id>] <短標題>`。最終則仍保留 `— backend · model · session:…` footer。

狀態：`queued` / `starting` / `running` / `completed` / `failed` / `interrupted`。

Bot 啟動時，先前仍是 queued/running/starting 的列會標成 **interrupted**，**不會**自動重跑或改 provider/model。訊息綁定與 session id 保留，可用回覆或 `/task <id> <說明>` 手動繼續。

SQLite 只存 task id、parent、worker key、backend、短標題、生命週期與 message id 綁定。**不**存完整 prompt、憑證或 worker 報告。

`/reset` 只清父 session，不清孤立任務。

Codex → Cursor 巢狀 worker 會掛在**目前任務**的 worker key 底下（`{task_key}:codex-cursor-worker`）。Codex reviewer prompt 被限制只審這一個任務／這一份報告，不得把歷史任務當成本次需求。既有安全限制（部署、重啟、migration、對外發送、金流等）不變。
