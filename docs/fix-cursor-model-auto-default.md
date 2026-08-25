# tg-cli-relay：Cursor 模型預設應固定 `auto`

> **用途**：交給另一台機器上的 agent 實作此 bug fix。  
> **專案路徑**：`tg-cli-relay`（本機範例：`/srv/tg-cli-relay` 或 `~/projects/tg-cli-relay`）  
> **狀態**：已在 `/srv/tg-cli-relay` 實作並通過測試；若目標機尚未合併，請依本文 patch。

---

## 問題描述

使用者回報：**tg-cli-relay（Cursor backend）的模型會被其它程式呼叫 Cursor CLI 時指定的 model 影響**。

例如：

- 其它腳本執行 `agent --model gemini-3.1-pro ...`
- Telegram relay 明明沒在 `/model` 指定，卻也跟著變成 pro

**預期行為**：

- Relay 預設一律使用 **`--model auto`**
- 只有以下情況才覆寫：
  1. 使用者在 Telegram 對該 thread 下 `/model <id>`（寫入 `model:cursor`）
  2. `.env` 設了 `TGR_CURSOR_MODEL=...`
- **不應**沿用舊版通用 preference key `model`（曾跨 provider 共用）
- **不應**在沒指定時省略 `--model`（省略時會落到 CLI 帳號預設或 `--resume` session 既有狀態）

---

## 根因（修前）

`src/tg_cli_relay/relay.py` 的 `_relay_cursor()` 舊邏輯：

```python
model = (
    store.get_pref(thread_key, "model:cursor")
    or store.get_pref(thread_key, "model")          # ← 會吃到其它 backend 設的通用 model
    or os.environ.get("TGR_CURSOR_MODEL", "").strip()
    or None                                         # ← None 時 CursorAgentProvider 不帶 --model
)
```

`src/tg_cli_relay/providers/cursor_agent.py`：

```python
if self.model:
    cmd.extend(["--model", self.model])
```

當 `model is None` 時，命令變成：

```bash
agent --print --trust --workspace ... --resume <chatId> "<prompt>"
```

此時模型由 Cursor CLI / 既有 chat 決定，容易與其它 CLI 呼叫或 session 狀態串在一起。

---

## 修復方案（摘要）

| 項目 | 修後 |
|------|------|
| Cursor 預設 | 固定 `auto` |
| CLI 參數 | **永遠**帶 `--model <resolved>` |
| 讀取順序 | `model:cursor` → `TGR_CURSOR_MODEL` → `auto` |
| 通用 `model` key | Cursor **不再讀取** |
| 其它 backend | `claude`/`codex`/`opencode` 也只讀 `model:<backend>`，不讀通用 `model` |

---

## 實作步驟

### 1. `src/tg_cli_relay/relay.py`

在 `_model_pref_key()` 之後新增：

```python
def resolve_cursor_model(store: SessionStore, thread_key: str) -> str:
    """Cursor relay 使用的模型；預設 auto，僅 thread 或 env 明確指定時才覆寫。"""
    import os

    explicit = store.get_pref(thread_key, _model_pref_key("cursor"))
    if not explicit:
        explicit = os.environ.get("TGR_CURSOR_MODEL", "").strip()
    if not explicit:
        return "auto"
    return "auto" if explicit.upper() == "AUTO" else explicit


def effective_model_for_backend(store: SessionStore, thread_key: str, backend: Backend) -> str:
    if backend == "cursor":
        return resolve_cursor_model(store, thread_key)
    model = store.get_pref(thread_key, _model_pref_key(backend))
    return model or "(預設)"
```

修改 `_relay_cursor()`：

```python
def _relay_cursor(store: SessionStore, thread_key: str, workspace: str, prompt: str) -> RunResult:
    import os

    bin_name = os.environ.get("TGR_CURSOR_AGENT_BIN", "agent").strip() or "agent"
    model = resolve_cursor_model(store, thread_key)
    prov = CursorAgentProvider(agent_bin=bin_name, model=model)
    # ...其餘不變
```

修改其它 backend，**移除** `or store.get_pref(thread_key, "model")`：

```python
# _relay_claude / _relay_opencode / _relay_codex
model = store.get_pref(thread_key, _model_pref_key("claude"))  # 依 backend 替換
```

### 2. `src/tg_cli_relay/telegram_bot.py`

```python
from tg_cli_relay.relay import effective_model_for_backend, relay_turn
```

將顯示模型的地方改為 `effective_model_for_backend(store, key, backend)`：

- `_format_model_catalog()` 內 `current = ...`
- `_cmd_status()` 的 `模型:` 那一行
- `_cmd_model()` 切換 provider 後顯示目前模型
- 回覆 footer：`model = effective_model_for_backend(...)`；`model != "(預設)"` 才 append

Cursor 沒特別指定時 `/status` 應顯示 **`auto`**。

### 3. `src/tg_cli_relay/cli.py`（doctor 輸出，選做）

```python
print(
    "TGR_CURSOR_MODEL =",
    os.environ.get("TGR_CURSOR_MODEL", "").strip() or "auto（relay 預設）",
)
```

### 4. 文件

- `README.md`：補 `TGR_CURSOR_MODEL` 說明；註明 Cursor relay 預設 `auto`、不讀通用 `model`
- `env.example`：加註 `# TGR_CURSOR_MODEL=auto`

### 5. 測試 `tests/test_cursor_model.py`

新增 unittest（至少涵蓋）：

1. 無 pref → `resolve_cursor_model` 回 `auto`
2. 只有通用 `model=gemini-3.1-pro` → 仍回 `auto`（**不回傳 gemini**）
3. `model:cursor=gpt-5.3-codex` → 回該值
4. `TGR_CURSOR_MODEL` env 覆寫
5. `thread pref` 優先於 env
6. `CursorAgentProvider(model="auto").run_turn(...)` 的 cmd 含 `--model auto`

執行：

```bash
cd /path/to/tg-cli-relay
source .venv/bin/activate   # 或對應 venv
python -m unittest tests.test_cursor_model -q
```

預期：`Ran 7 tests ... OK`

---

## 部署

```bash
# 1. 合併程式碼後重裝（若用 editable install）
pip install -e ".[telegram]"

# 2. 重啟 relay（依實際 service 名稱）
sudo systemctl restart tg-cli-relay
# 若有第二組實例：
# sudo systemctl restart tg-cli-relay-group2

# 3. 確認程序起來
systemctl is-active tg-cli-relay
```

**不必**改 SQLite 結構；舊的通用 `model` 列可留著，只是 Cursor 不再讀。

若使用者曾被設成 pro，可在 Telegram 手動：

```
/model auto
```

---

## 驗收清單

- [ ] `python -m unittest tests.test_cursor_model -q` 全過
- [ ] `python -m tg_cli_relay doctor` 顯示 `TGR_CURSOR_MODEL = auto（relay 預設）`（未設 env 時）
- [ ] Telegram `/status` 在 Cursor backend、未 `/model` 時顯示 **`模型: auto`**
- [ ] 在 DB `preferences` 僅有 `model=gemini-3.1-pro`、無 `model:cursor` 時，relay 仍用 **auto**（可看 log 或暫時在 provider 印 cmd）
- [ ] `/model gpt-5.3-codex` 後下一輪生效
- [ ] 其它機器跑 `agent --model gemini-3.1-pro ...` **不會**改變 relay 預設（除非使用者自己在 relay `/model`）

### 手動驗證 Cursor 命令（可選）

在 relay log 或暫時 debug 確認實際命令含：

```bash
agent --print --trust --workspace <ws> --model auto --resume <chatId> "<prompt>"
```

---

## 相關知識庫（可選更新）

若該環境有 `/srv/docker/knowledge/hms-llm-fallback.md`，其中「Cursor 額度判斷坑」已提到：

- relay 沒帶 `--model` 時行為不確定
- HMS 要用 auto 必須**明確** `--model auto`

本 fix 讓 tg-cli-relay 與該文件建議一致。

---

## 變更檔案一覽

| 檔案 | 動作 |
|------|------|
| `src/tg_cli_relay/relay.py` | 核心：resolve + 預設 auto |
| `src/tg_cli_relay/telegram_bot.py` | 狀態顯示 |
| `src/tg_cli_relay/cli.py` | doctor 輸出（選做） |
| `tests/test_cursor_model.py` | 新增 |
| `README.md` | 文件 |
| `env.example` | 文件 |

**不需改** `providers/cursor_agent.py`（已有 `if self.model: cmd.extend(["--model", ...])`；修 relay 讓 model 永遠有值即可）。

---

## Agent 實作提示

- 範圍保持最小：只動模型解析與顯示，不要順便改 Telegram UX
- 不要恢復通用 `model` fallback（那是 bug 來源）
- Cursor 預設必須是字串 `"auto"`，不是 `None`
- 合併後務必 **restart systemd**，否則舊 process 仍跑舊 code
