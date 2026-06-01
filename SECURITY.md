# Security Policy

`tg-cli-relay` connects Telegram messages to local coding-agent CLIs. Because those agents may read and write files, run commands, or access credentials, deployments should be treated as security-sensitive.

## Supported versions

This project is currently pre-`1.0`. Security fixes are applied to the `main` branch until tagged releases are available.

## Reporting a vulnerability

Please open a GitHub issue with a clear description of the risk and a minimal reproduction when it is safe to disclose publicly.

Do not include bot tokens, API keys, OAuth tokens, private repository URLs, or other secrets in issues.

## Deployment recommendations

- Always set `TGR_ALLOWED_TELEGRAM_USER_IDS` to restrict who can use the bot.
- Run the relay under a dedicated operating-system user with the minimum required permissions.
- Use a dedicated workspace instead of pointing the relay at your home directory.
- Keep `TELEGRAM_BOT_TOKEN`, API keys, OAuth tokens, and CLI credentials out of git.
- Prefer provider defaults that keep approvals and sandboxing enabled.
- Only enable bypass or skip-permission modes inside an isolated environment.
- Review generated changes before merging them into important repositories.

## High-risk options

The following options intentionally reduce safety checks and should be used only in controlled environments:

- `TGR_CODEX_BYPASS_APPROVALS_AND_SANDBOX=1`
- `TGR_CLAUDE_SKIP_PERMISSIONS=1`

If these options are enabled, isolate the workspace, restrict the Telegram allowlist, and avoid storing production secrets in the same user account.

## Design notes

The relay stores provider session identifiers and thread preferences. It does not intentionally store full conversation history; history is handled by the underlying CLI providers.
