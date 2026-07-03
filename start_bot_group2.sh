#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="/srv/tg-cli-relay"
ENV_FILE="${BASE_DIR}/deploy/env.group2-claude-codex"
PYTHON_BIN="${BASE_DIR}/.venv/bin/python3"
PID_FILE="${BASE_DIR}/run/group2.pid"
LOG_FILE="${BASE_DIR}/data/group2/bot.log"

mkdir -p "${BASE_DIR}/data/group2" "${BASE_DIR}/run"
"${BASE_DIR}/deploy/check-group2-ready.sh"

if [[ -f "${PID_FILE}" ]]; then
  old_pid="$(cat "${PID_FILE}")"
  if [[ -n "${old_pid}" ]] && kill -0 "${old_pid}" 2>/dev/null; then
    echo "第二組 Bot 已在執行中 (PID: ${old_pid})"
    exit 0
  fi
  rm -f "${PID_FILE}"
fi

SECRETS_FILE="${BASE_DIR}/deploy/env.group2.secrets"

cd "${BASE_DIR}"
set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
if [[ -f "${SECRETS_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${SECRETS_FILE}"
fi
set +a

nohup "${PYTHON_BIN}" -m tg_cli_relay bot >> "${LOG_FILE}" 2>&1 &
new_pid=$!
echo "${new_pid}" > "${PID_FILE}"

sleep 1
if kill -0 "${new_pid}" 2>/dev/null; then
  echo "第二組 Bot 啟動成功 (PID: ${new_pid})"
  echo "日誌：${LOG_FILE}"
else
  echo "第二組 Bot 啟動失敗，請查看：${LOG_FILE}"
  exit 1
fi
