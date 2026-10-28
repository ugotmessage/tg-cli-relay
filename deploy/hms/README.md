# HMS watcher integration snapshot

This directory versions the deployed shared-watcher integration for the Relay callback repair. It contains source/configuration templates only, no credentials or runtime state. The Relay, outbox, receivers, database migration and tests are in the rest of this repository.

- `cursor-worker-watchdog.py`: deployed watcher, including terminal timeout classification and callback enqueue/drain. Install at the watcher path used by the service.
- `run-cursor-worker-watchdog.sh.example`: two-bot receiver allowlist and existing service launcher. It contains HMS host paths; edit paths for the destination host before installation. Secret *paths* are examples, not secret values.
- `manifest.json`: SHA-256 hashes of the deployed integration snapshot.

The watcher depends on existing HMS facilities: `skills/autonomous-ai-agents/cursor-agent/scripts/run.py`, `scripts/notify.py`, Cursor CLI authentication, its allowed workspace roots and shared queue. These dependencies belong to the HMS installation and are not vendored here. This snapshot is not a standalone portable installer. See `../callback-receive-local.sh.example`, `../../tools/hms_callback_outbox.py` and `../../docs/background-callback.md`.

## Both-bot deployment

1. Install the repo/venv and dependencies. Set per-bot `TGR_SESSION_DB`, `TGR_DEFAULT_WORKSPACE`, token and account-specific Codex executable outside Git.
2. Set `TGR_BACKGROUND_HANDOFF=1`, a distinct `TGR_BACKGROUND_HANDOFF_STATE_ROOT` for each bot, the protected callback signing-secret path, and `TGR_BACKGROUND_HANDOFF_QUEUE_ROOT` pointing to the existing shared watcher queue. State roots must be **outside every enclosing Git repository as resolved from the actual service cwd**, not just outside the Relay source checkout. For HMS secondary the working configuration is `/Users/ht/.local/state/tg-cli-relay/codex2-callback`; the old `.hermes/codex-relay/data/background-callback` was rejected because `.hermes` is a Git repository.
3. Install `tools/hms_callback_outbox.py` and the receiver wrapper at the paths configured by the existing watcher. Configure `HMS_CALLBACK_LOCAL_RECEIVER_ROUTES` with both named receiver commands. Do not simultaneously set the single `HMS_CALLBACK_LOCAL_RECEIVER_ARGV`. Each command loads its own bot env and Python runtime. Probes select exactly one route; finals must never be broadcast.
4. Preserve DBs, callback state, sessions and env before serial restarts. Review uncertain in-flight work; never blindly rerun it or restore an old DB over newer state. Stop the old host's bot consumer before a host cutover.
5. Validate from each service's actual cwd that `resolve_state_root()` accepts its configuration. Run repository tests and verify per-bot real CLI detach/resume, signed callback, original-session continuation, Telegram delivery, no cross-bot event, unchanged main-session mapping and empty/unpaused queues.

Secrets, actual `.env`, authentication, databases, sessions and logs are provisioned/transferred separately. Never commit them. Reusing this deployment on another host requires adapting filesystem paths, service ownership and credential provisioning.

## Validation and limits

The deployed Relay revision passed 252 tests and 8 subtests under controller verification. Actual account-specific Codex CLI detach/resume and signed production callback through both live bots were verified; both delivered their own Telegram test reply without changing main-session mappings. Temporary worker-state fixtures modeled the background worker lifecycle; no additional long-running Cursor job was launched for acceptance.

Known limits remain: a task completing before marker registration can fail closed as unknown task, and a remote receiver without access to a verifiable submission queue cannot perform local handoff detach verification. This release does not claim either case is repaired. An interrupted background continuation is retained for review and is not silently replayed.
