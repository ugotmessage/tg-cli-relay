#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="/srv/tg-cli-relay"
PID_FILE="${BASE_DIR}/run/group2.pid"

if [[ ! -f "${PID_FILE}" ]]; then
  echo "找不到 PID 檔，可能未啟動：${PID_FILE}"
  exit 0
fi

pid="$(cat "${PID_FILE}")"
if [[ -z "${pid}" ]]; then
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
  kill -0 "${pid}" 2>/dev/null || break
  sleep 1
done

if kill -0 "${pid}" 2>/dev/null; then
  kill -9 "${pid}" || true
fi

rm -f "${PID_FILE}"
echo "第二組 Bot 已停止 (PID: ${pid})"
