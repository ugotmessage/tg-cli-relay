# Telegram ambiguous task resolution (no MCP)

The Relay does **not** require a separate classifier model or MCP server.

## Routing contract

1. **Deterministic first:** Telegram Reply linked to a task, Reply linked to the
   main session, explicit Task ID and manual selection keep their existing
   routing. No LLM may override an explicit binding.
2. **Cheap admission check:** With existing jobs, only unbound messages that
   look like a task follow-up or mention an identifiable task title request
   semantic resolution. Explicit new delegate/coding requests retain the
   original background-task Router and FIFO queue. Ordinary unrelated chat
   does not call the resolver.
3. **Primary CLI:** Relay obtains up to 8 task IDs, redacted titles, and status
   values **from the same Telegram chat/topic**. It sends a short routing-only
   user turn to the active Codex / Cursor / Claude / OpenCode CLI, using its
   existing main Session. It does not override the system prompt and does not
   call another model/provider. This may add one *extra primary-provider CLI
   turn* when a follow-up is ambiguous.
4. **Strict response:** The only valid output is one JSON object:
   - {"action":"task","task_id":"existing exact id"}
   - {"action":"new"}
   - {"action":"ask"}
   Any malformed output, model error, unknown task, or wrong conversation ID
   is treated as "ask" rather than guessing.
5. **Relay controls dispatch:** Only Relay maps a validated Task ID to the
   target provider/session and queues follow-up messages if the worker is busy.
   The routing CLI never receives privileges to mutate TaskRegistry directly.
   "new" continues the original text via the ordinary Router/main conversation.
   Task progress queries do not accidentally re-run selected workers.

Routing-only turns skip Codex-to-Cursor auto-delegation and Cursor harness
prefixes. Provider CLI sessions remain otherwise unchanged: a routing-only
prompt is **not a security sandbox** or a guarantee that agent tools will be
disabled, so never enable unrestricted tool permissions for untrusted chats.

## Configuration

Task context resolution is enabled by default. To roll back independently of
App Server and TaskRegistry:

```dotenv
TGR_TASK_CONTEXT_RESOLVER=0
```

When disabled, ambiguous continuations fall back to the older candidate-list
behavior. Existing Reply and Task ID routing still work. Neither this feature
nor the previous App Server work implements the proposed 120-second automatic
main-turn-to-background transition.

## Examples

With task A = "分點補檔" and task B = "三箭報告":

- Reply to Task A → exact task A, no classifier
- "補檔那邊不要全部重跑" → ask current CLI to choose A / new / ask
- "請實作第二個獨立功能" → new Task through normal Router
- "進度" with several tasks and no explicit focus → primary CLI chooses which
  *status* to read, not which worker to rerun
- "今天吃什麼" → ordinary main conversation, no classifier
