# Roadmap

`tg-cli-relay` is an early-stage open-source relay for operating local coding-agent CLIs from Telegram. The project is designed for solo maintainers and small teams that want to keep credentials local while still being able to continue agent sessions remotely.

## Near term

- Add provider health checks for `codex`, `claude`, `cursor`, and `opencode`.
- Add automated tests for provider switching, session persistence, and Telegram command parsing.
- Improve error messages when a CLI is missing, unauthenticated, or times out.
- Add safer defaults for workspaces, environment variables, and bot permissions.

## Security and safety

- Add an optional approval mode before running dangerous agent profiles.
- Document safe deployment patterns for a dedicated user account, isolated workspace, and restricted Telegram allowlist.
- Add examples for systemd hardening and macOS LaunchAgent troubleshooting.
- Review subprocess handling, shell argument passing, and environment-variable leakage paths.

## Deployment and maintainability

- Add Docker and Compose examples for server deployment.
- Add release notes and tagged versions starting from `v0.1.0`.
- Add a minimal CI workflow for linting and tests.
- Add example workflows for maintaining open-source repositories through Codex and other CLI agents.

## Long term

- Add per-thread workspace isolation helpers.
- Add provider usage logging without storing conversation content.
- Add pluggable transport support beyond Telegram.
- Add a small web status page that exposes health and configuration status without secrets.
