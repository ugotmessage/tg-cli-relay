# Codex App Server transport and worker choice

This is an **opt-in** implementation. Upgrading the repository alone does not
start App Server or change an existing `codex exec` production bot.

## Configuration

```dotenv
TGR_BACKEND=codex
TGR_CODEX_BIN=codex
TGR_CODEX_TRANSPORT=app-server
TGR_CODEX_APP_TURN_TIMEOUT=7200
TGR_CODEX_APP_STEER=1
TGR_CODEX_APP_SANDBOX=workspace-write
TGR_WORKER_PROVIDER=auto
```

The bot uses the logged-in Codex CLI account on the **same OS user account**
that launches the bot. App Server is spawned via
`codex app-server --listen stdio://`; it does not require another API key.
Use a current Codex binary that supports `thread/start`, `thread/resume`,
`turn/start`, `turn/steer`, and `turn/interrupt`.

- **Transport:** `cli` (default) remains the old `codex exec` path;
  `app-server` starts one persistent per-binary process and keeps native
  Codex thread IDs in existing SessionStore. No database migration required.
- **Turn timeout:** `TGR_CODEX_APP_TURN_TIMEOUT` defaults to 7200 seconds;
  zero disables the turn deadline. On timeout the relay attempts
  `turn/interrupt` and records an error rather than falsely reporting success.
- **Steer:** only an explicit Telegram Reply to a message already bound to
  the **main Codex session** can steer a turn in progress. A failed or raced
  `turn/steer` request follows the old FIFO queue. Replies to Task messages
  still route to that task. Unbound old Reply messages retain conservative
  routing and never guess a Task.
- **Approvals:** the unattended bot uses `approvalPolicy=never` and
  `workspace-write` sandbox by default; interactive server approval requests
  are **declined**, not automatically approved. Never enable the existing
  `TGR_CODEX_BYPASS_APPROVALS_AND_SANDBOX` switch for an untrusted workspace.
- **Model:** `/model codex <id>` still sets Codex's own model; App Server
  receives this model in `thread/start` / `turn/start`. Available native
  subagent models/capabilities depend on the installed Codex version.

## Worker preferences

```dotenv
TGR_WORKER_PROVIDER=auto
# auto | native | off | cursor | claude | opencode
```

- `auto`: use Cursor as an external worker **only if its executable exists**;
  otherwise let Codex choose native subagents/models when available, or handle
  the request itself.
- `native` or `codex`: delegate decisions to the active Codex agent using
  its own available tools/model catalog; **does not guarantee** that Codex will
  spawn a native subagent.
- `off`: do not automatically spawn an external worker or add delegation
  instructions. Existing manual `/task` background routing still works.
- `cursor`, `claude`, `opencode`: prefer that executable if installed,
  otherwise use Codex natively. External Cursor and Claude worker sessions
  use task-scoped IDs. OpenCode's `-c` resumes the last workspace session, so
  this adapter intentionally creates a fresh session each worker invocation
  to avoid cross-task history leakage.
- Deprecated `TGR_CODEX_DELEGATE_TO_CURSOR=1/0` is respected when no new
  `TGR_WORKER_PROVIDER` setting exists; an explicit `0` preserves the old
  behavior of disabling external delegation.

The relay has no built-in model router and does not charge a separate LLM
request to classify messages. Task identity is selected via Telegram Reply,
Task ID and TaskRegistry before any provider execution.

## Rollout and recovery

1. Update Codex CLI and confirm `codex app-server --help` works with the bot user.
2. Test in a staging Telegram bot; ensure that its session DB is backed up.
3. Set `TGR_CODEX_TRANSPORT=app-server`, restart the bot, and send a short
   simple message. Then Reply to that main-thread message during a long turn
   to exercise steering.
4. If the app-server protocol is unsupported or behaves unexpectedly, switch
   back to `TGR_CODEX_TRANSPORT=cli` and restart. Existing provider session
   records are not discarded. Tasks that were running during restart become
   `interrupted` and require a user-driven continuation.

This release includes protocol-level mocked tests, not a live host-to-Codex
or end-to-end Telegram deployment test. A bot restart is still necessary.
