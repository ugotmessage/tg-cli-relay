#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="/srv/tg-cli-relay/deploy/env.group2-claude-codex"
SECRETS_FILE="/srv/tg-cli-relay/deploy/env.group2.secrets"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "缺少環境檔：${ENV_FILE}（請 cp deploy/env.group2-claude-codex.example）" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
if [[ -f "${SECRETS_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${SECRETS_FILE}"
fi
set +a

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  echo "請在 ${ENV_FILE} 設定 TELEGRAM_BOT_TOKEN（第二組專用 bot）" >&2
  exit 1
fi

GROUP1_ENV="/srv/tg-cli-relay/.env"
if [[ -f "${GROUP1_ENV}" ]]; then
  group1_token="$(grep -E '^TELEGRAM_BOT_TOKEN=' "${GROUP1_ENV}" | cut -d= -f2- | tr -d '\r' || true)"
  if [[ -n "${group1_token}" && "${TELEGRAM_BOT_TOKEN}" == "${group1_token}" ]]; then
    echo "第二組 TELEGRAM_BOT_TOKEN 不可與第一組相同" >&2
    exit 1
  fi
fi

if [[ "${TGR_BACKEND:-}" == "cursor" ]] || [[ "${TGR_ENABLED_BACKENDS:-}" == *cursor* ]]; then
  agent_bin="${TGR_CURSOR_AGENT_BIN:-agent}"
  if ! command -v "${agent_bin}" >/dev/null 2>&1; then
    echo "找不到 Cursor agent CLI：${agent_bin}" >&2
    exit 1
  fi
  if [[ -z "${CURSOR_API_KEY:-}" ]]; then
    if ! HOME="${HOME:-/root}" "${agent_bin}" --list-models >/dev/null 2>&1; then
      echo "Cursor 未認證：請在 ${SECRETS_FILE} 設定 CURSOR_API_KEY，或執行 agent login" >&2
      exit 1
    fi
  fi
fi
