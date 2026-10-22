# Telegram 任務隔離（task isolation）

每個背景任務使用獨立 CLI session（`{parent}:job:{task_id}`），並把 Telegram 訊息綁到該任務。回覆任務相關訊息會繼續**同一個**任務 session，不會因為另一個任務正在跑、或 bot 重啟，就掉回混在一起的父對話。

資料存在該 bot 既有的 SQLite（`TGR_SESSION_DB`）裡，只新增 `tasks` / `task_messages` 兩張表，不改 `sessions` / `preferences`。

## 怎麼選任務（確定性，沒有 LLM 分類）

| 輸入 | 行為 |
|---|---|
| 尚未有任何任務的普通文字 | 與以前相同：DIRECT 走父 session；DELEGATE 開新孤立任務 |
| 新的 DELEGATE 訊息 | **一定**開新任務與新 session，不重用其它任務或父 session。即使內文提到「進度」也是新任務 |
| `/task new <說明>` | 同樣開新孤立任務；小型 DIRECT 題目也可以，不會灌入舊的父對話歷史 |
| 回覆已綁定的來源／ACK／進度／結果／附件／錯誤／狀態 | 送到**該則訊息所屬任務**的 session |
| 回覆已綁定訊息且整句為進度查詢 | 只回該任務狀態，**不**重跑 provider |
| `/task <id>` | 顯示該任務狀態；這則狀態回覆會綁定同一任務，回覆它可以繼續 |
| `/task <id> <說明>` | 把說明送到該任務 session |
| `/task` | 列出本對話最近任務 |
| 未綁定的回覆，且本對話已有任務 | **不**呼叫 provider；請回覆 `[task …]` 訊息或 `/task <id>` |
| 未串接且**整句**為延續用語（`繼續`、`接著做`、`往下做` 等）且有多個任務 | **不**呼叫 provider、**不**走父 session；列出任務請選擇 |
| 上述延續用語且只有一個明確任務 | 恢復該任務（把該用語送到該 session） |
| 未串接且**整句**為進度查詢（`進度`、`現在進度如何`、`做好了嗎` 等） | 只回狀態，不重跑；有多個任務時改為請選擇 |
| 無關的新問題（不是回覆、不是 `/task`、不是整句延續／進度） | 不當成延續。DIRECT 走父對話；DELEGATE 開新任務 |
| 舊的未綁定父對話回覆 | **不會**自動猜測或遷移；請用 `/task new <說明>` 一次建立孤立任務後再回覆 |
| 未知、他對話／他主題、或不存在的 task id | 明確失敗，**絕不**默默落到父對話 |

延續／進度只用**整句錨定**規則（例如整則訊息就是 `繼續`），不用子字串去搶「請繼續實作登入…」這類新請求。

跨 chat、跨 forum topic、跨 bot DB 的訊息與任務互不相通。任務執行時使用**建立時**記下的 backend 與該任務的 worker key；父對話當下的 `/provider` 不會把別人的 session 接過來。建立任務時會把父 thread 的 `model:{backend}` 偏好**複製**到 worker key，之後互不污染。

## 排隊

- 父對話的新 DELEGATE / `/task new` 仍維持原本的順序與忙碌排隊，主對話 DIRECT 在背景任務執行期間仍可回應。
- 對**同一任務**的 follow-up 在訊息到達當下就凍住 `task_id` / `worker_key` / `backend`，不會等到 dequeue 才重判。
- 同一任務 session 同一時間只會寫入一輪；後到的 follow-up 等上一輪結束。父佇列的初始請求必須先執行，該任務的 follow-up 才會跑。
- 若登錄裡是 `queued` 但記憶體裡沒有原始請求（例如測試探測或程序已重啟後的殘列），**不會**自動跑 follow-up，會提示手動 `/task <id> <說明>`。
- 不同任務的 session 可以並行（例如回覆已完成的 A，同時 B 還在跑）。

## 顯示與生命週期

所有任務的 ACK、進度、狀態、最終分段、佇列已滿、附件失敗、錯誤都會帶 `[task <id>] <短標題>`。最終則仍保留 `— backend · model · session:…` footer。標題是短路由名；若 prompt 看起來含路徑或憑證賦值，改存通用標題 `background task`，不把原始路徑／憑證當標題回顯。**沒有**全面密文掃描。

狀態：`queued` / `starting` / `running` / `completed` / `failed` / `interrupted`。

Bot 啟動時，先前仍是 queued/running/starting 的列會標成 **interrupted**，**不會**自動重跑或改 provider/model。訊息綁定與 session id 保留，可用回覆或 `/task <id> <說明>` 手動繼續。

任務登錄只存 task id、parent、worker key、backend、短標題、生命週期與 message id 綁定，不存完整 prompt 或 worker 報告。另有下述有界上下文，儲存在既有 `preferences`。

`/reset` 清父 session 與父對話交接資料，不清孤立任務。父 session 執行或排隊時拒絕重置；重置後，先前任務的晚到結果不會重新灌入父對話。

Codex → Cursor 每次派工建立新的 Worker session，明確傳遞目前任務的上下文，不再 resume 舊的巢狀 Worker。Codex reviewer session 仍維持同一任務，審核目前請求與報告。既有安全限制（部署、重啟、migration、對外發送、金流等）不變。

## 上下文交接

- 父對話及任務每輪成功完成後，保存當輪問題與最終結果的有界摘錄。新任務取得建立時已完成的父對話快照；之後只加入該任務自己的成功回合，不讀取其他任務後續進度。
- 任務成功結果以 task ID 標示，原子追加到父對話參考資料。父 session 下一輪才會讀取，不在背景直接寫入／呼叫父 CLI，因此不與父對話競爭同一 provider session。
- Router 仍只判斷當輪原始問題；前文不影響 DIRECT／DELEGATE 或任務路由。前文以 JSON 參考資料提供，明確禁止把舊問題當新指令執行。
- 使用既有 `preferences` 的 `relay:context:v1`，不額外建立資料表。每份紀錄最多 8 輪、16,000 JSON 字元；單輪問題最多 2,000 字元、結果最多 4,000 字元（另加截短標記）。任務最多另帶一份父對話快照。
- 僅保存成功結果，不保存 stderr、進度、Worker 報告；常見憑證賦值／私鑰提示的整段內容會略過，附件輸出標記不保留。這不是完整的敏感資訊掃描，session DB 仍需依既有方式保護。
- reset 以 generation 隔離；並行任務回寫使用同一 SQLite transaction，避免遺失更新或使舊記憶復活。
- 更新前的 CLI 歷史／job 不自動匯入；交接資料從更新後成功問答開始累積。快照不含建立時尚未完成的父對話內容，長文也可能截短，不能當成完整逐字歷史或最新事實。
