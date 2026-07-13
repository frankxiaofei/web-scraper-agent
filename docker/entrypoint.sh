#!/usr/bin/env bash
# 智慧农业 8091 + 股票 8092 + 可选 scheduler
set -euo pipefail

cd /app
mkdir -p logs data

HOST="${HOST_UI:-0.0.0.0}"
ENABLE_SCHEDULER="${ENABLE_SCHEDULER:-true}"

PIDS=()

log() {
  echo "[entrypoint] $*"
}

cleanup() {
  log "收到停止信号，结束子进程..."
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}

trap cleanup SIGTERM SIGINT

start_proc() {
  local name=$1
  shift
  "$@" >> "logs/${name}.log" 2>&1 &
  PIDS+=("$!")
  log "已启动 ${name} (pid $!, 日志 logs/${name}.log)"
}

start_proc agri_ui python scripts/run_agri_ui.py --host "$HOST" --port 8091
start_proc stock_ui python scripts/run_stock_ui.py --host "$HOST" --port 8092

if [ "${ENABLE_SCHEDULER}" = "true" ]; then
  start_proc scheduler python scripts/run_scheduler.py
else
  log "ENABLE_SCHEDULER=false，跳过调度器"
fi

log "服务就绪：8091 农业 | 8092 股票"

while true; do
  if ! wait -n; then
    status=$?
    log "子进程异常退出 (code=${status})"
    cleanup
    exit "${status}"
  fi
done
