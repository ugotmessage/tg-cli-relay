#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="/srv/tg-cli-relay"
PYTHON_BIN="${BASE_DIR}/.venv/bin/python3"
LOG_FILE="${BASE_DIR}/bot.log"
PID_DIR="${BASE_DIR}/run"
PID_FILE="${PID_DIR}/bot.pid"

mkdir -p "${PID_DIR}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "找不到 Python: ${PYTHON_BIN}"
  echo "請先建立虛擬環境：python3 -m venv .venv"
  exit 1
fi

if [[ -f "${PID_FILE}" ]]; then
  old_pid="$(cat "${PID_FILE}")"
  if [[ -n "${old_pid}" ]] && kill -0 "${old_pid}" 2>/dev/null; then
    echo "Bot 已在執行中 (PID: ${old_pid})"
    exit 0
  fi
  rm -f "${PID_FILE}"
fi

cd "${BASE_DIR}"
nohup env PYTHONPATH=src "${PYTHON_BIN}" -m tg_cli_relay bot > "${LOG_FILE}" 2>&1 &
new_pid=$!
echo "${new_pid}" > "${PID_FILE}"

sleep 1
if kill -0 "${new_pid}" 2>/dev/null; then
  echo "Bot 啟動成功 (PID: ${new_pid})"
  echo "日誌：${LOG_FILE}"
else
  echo "Bot 啟動失敗，請查看日誌：${LOG_FILE}"
  exit 1
fi
