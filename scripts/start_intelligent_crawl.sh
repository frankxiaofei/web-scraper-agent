#!/usr/bin/env bash
# 一键启动全部 enabled 站点的智能爬取（本地 Chrome + MongoDB）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

source .venv/bin/activate
unset PLAYWRIGHT_BROWSERS_PATH

MONGO_CONTAINER="web_scraper_mongo"
if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$MONGO_CONTAINER"; then
  echo "警告: MongoDB 容器 ($MONGO_CONTAINER) 未运行。" >&2
  echo "请先启动: docker compose -f docker/docker-compose.yml up -d mongo" >&2
  read -r -p "是否仍继续爬取（数据可能无法写入 MongoDB）？[y/N] " reply
  if [[ ! "$reply" =~ ^[Yy]$ ]]; then
    echo "已取消。"
    exit 1
  fi
fi

STAMP="$(date +%Y%m%d_%H%M%S)"
LOG="data/intelligent_crawl_${STAMP}.log"
mkdir -p data

echo "started_at=$(date -Iseconds)" | tee "$LOG"
echo "日志: $LOG" | tee -a "$LOG"

bash scripts/run_with_local_chrome.sh python scripts/run_all_enabled.py \
  --intelligent \
  --max-items 10 \
  2>&1 | tee -a "$LOG"

echo "finished_at=$(date -Iseconds)" | tee -a "$LOG"
