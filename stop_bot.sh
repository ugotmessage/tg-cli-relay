#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="/srv/tg-cli-relay"
PID_FILE="${BASE_DIR}/run/bot.pid"

if [[ ! -f "${PID_FILE}" ]]; then
  echo "找不到 PID 檔，可能未啟動：${PID_FILE}"
  exit 0
fi

pid="$(cat "${PID_FILE}")"
if [[ -z "${pid}" ]]; then
  echo "PID 檔為空，移除後結束。"
  rm -f "${PID_FILE}"
  exit 0
fi

if ! kill -0 "${pid}" 2>/dev/null; then
  echo "程序不存在 (PID: ${pid})，移除 PID 檔。"
  rm -f "${PID_FILE}"
  exit 0
fi

kill "${pid}"
for _ in {1..15}; do
  if kill -0 "${pid}" 2>/dev/null; then
    sleep 1
  else
    break
  fi
done

if kill -0 "${pid}" 2>/dev/null; then
  echo "程序仍在執行，送出 SIGKILL (PID: ${pid})"
  kill -9 "${pid}" || true
fi

rm -f "${PID_FILE}"
echo "Bot 已停止 (PID: ${pid})"
