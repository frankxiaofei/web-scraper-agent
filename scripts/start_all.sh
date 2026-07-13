#!/usr/bin/env bash
# 一键启动 MongoDB、Web UI、调度器与后台全站智能爬取。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

HOST="${WEB_UI_HOST:-127.0.0.1}"
REQUESTED_PORT="${WEB_UI_PORT:-8080}"
MONGO_CONTAINER="web_scraper_mongo"

port_in_use() {
  local p="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"$p" -sTCP:LISTEN -P -n >/dev/null 2>&1
  else
    (echo >/dev/tcp/"$HOST"/"$p") >/dev/null 2>&1
  fi
}

pick_port() {
  local p="$1"
  if ! port_in_use "$p"; then
    echo "$p"
    return 0
  fi
  for alt in 8081 8090 8766 8877; do
    if ! port_in_use "$alt"; then
      echo "警告: 端口 $p 已被占用，改用 $alt" >&2
      echo "$alt"
      return 0
    fi
  done
  echo "错误: 无可用 Web UI 端口（尝试过 $p 8081 8090 8766 8877）" >&2
  exit 1
}

PORT="$(pick_port "$REQUESTED_PORT")"
COMPOSE_FILE="${ROOT}/docker/docker-compose.yml"

if [[ ! -d "${ROOT}/.venv" ]]; then
  echo "错误: 未找到 .venv，请先 bash scripts/install.sh" >&2
  exit 1
fi

# shellcheck disable=SC1091
source "${ROOT}/.venv/bin/activate"
unset PLAYWRIGHT_BROWSERS_PATH
# Python 进程通过 pydantic-settings 读取 .env（路径含空格时勿 bash source）

mkdir -p "${ROOT}/data"
STAMP="$(date +%Y%m%d_%H%M%S)"
WEB_UI_LOG="${ROOT}/data/web_ui_${STAMP}.log"
SCHEDULER_LOG="${ROOT}/data/scheduler_${STAMP}.log"
CRAWL_LOG="${ROOT}/data/crawl_${STAMP}.log"

echo "==> 1/4 检查 MongoDB Docker 容器"
if ! command -v docker >/dev/null 2>&1; then
  echo "警告: 未安装 docker，跳过 Mongo 启动（数据可能无法写入 MongoDB）。" >&2
elif docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$MONGO_CONTAINER"; then
  echo "MongoDB 容器已运行: $MONGO_CONTAINER"
else
  echo "启动 MongoDB: docker compose -f docker/docker-compose.yml up -d mongo"
  docker compose -f "$COMPOSE_FILE" up -d mongo
  for _ in $(seq 1 30); do
    if docker inspect --format='{{.State.Health.Status}}' "$MONGO_CONTAINER" 2>/dev/null | grep -qx healthy; then
      echo "MongoDB 健康检查通过"
      break
    fi
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$MONGO_CONTAINER"; then
      if docker exec "$MONGO_CONTAINER" mongosh --quiet --eval "db.adminCommand('ping').ok" 2>/dev/null | grep -qx 1; then
        echo "MongoDB ping 成功"
        break
      fi
    fi
    sleep 1
  done
fi

echo "==> 2/4 停止旧的 Web UI / 调度器进程"
pkill -f "[p]ython.*scripts/run_web_ui.py" 2>/dev/null || true
pkill -f "[p]ython.*scripts/run_scheduler.py" 2>/dev/null || true
sleep 1

echo "==> 3/4 后台启动 Web UI 与调度器"
nohup python "${ROOT}/scripts/run_web_ui.py" --host "$HOST" --port "$PORT" \
  >>"$WEB_UI_LOG" 2>&1 &
WEB_UI_PID=$!
echo "Web UI PID=$WEB_UI_PID 日志: $WEB_UI_LOG"

nohup bash "${ROOT}/scripts/run_with_local_chrome.sh" \
  python "${ROOT}/scripts/run_scheduler.py" \
  >>"$SCHEDULER_LOG" 2>&1 &
SCHEDULER_PID=$!
echo "调度器 PID=$SCHEDULER_PID 日志: $SCHEDULER_LOG"

echo "==> 4/4 后台启动全站智能爬取 (--intelligent --max-items 10)"
nohup bash "${ROOT}/scripts/run_with_local_chrome.sh" \
  python "${ROOT}/scripts/run_all_enabled.py" --intelligent --max-items 10 \
  >>"$CRAWL_LOG" 2>&1 &
CRAWL_PID=$!
echo "爬取 PID=$CRAWL_PID 日志: $CRAWL_LOG"

BASE="http://${HOST}:${PORT}"
echo ""
echo "========== 已启动 =========="
echo "Web UI:     ${BASE}/"
echo "Agent 面板: ${BASE}/agent"
echo "Agent 日志: ${BASE}/agent/logs"
echo "同步面板:   ${BASE}/sync"
echo "API 状态:   ${BASE}/api/sync/status"
echo ""
echo "日志:"
echo "  Web UI:   $WEB_UI_LOG"
echo "  调度器:   $SCHEDULER_LOG"
echo "  全站爬取: $CRAWL_LOG"
echo "  Agent 爬取: ${ROOT}/data/agent-crawl.log"
echo ""
echo "查看爬取: tail -f \"$CRAWL_LOG\""
echo "查看 Agent: tail -f \"${ROOT}/data/agent-crawl.log\""
echo "停止服务: pkill -f 'python.*scripts/run_web_ui.py'; pkill -f 'python.*scripts/run_scheduler.py'"
echo ""
echo "可选 — Hermes Crawl Agent 守护进程（另起终端，不随 start_all 自动启动）："
echo "  nohup python \"${ROOT}/scripts/hermes_crawl_client.py\" --daemon \\"
echo "    --interval-minutes 15 --auto-reset >>\"${ROOT}/data/agent-crawl.log\" 2>&1 &"
echo "  # 或单次巡检: python \"${ROOT}/scripts/hermes_crawl_client.py\" --once"
