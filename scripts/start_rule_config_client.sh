#!/usr/bin/env bash
# 启动爬取规则配置桌面客户端（Electron + 内嵌 Chrome）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

API_PORT="${WEB_UI_PORT:-8090}"
API_HOST="${WEB_UI_HOST:-127.0.0.1}"
export WEB_SCRAPER_API="http://${API_HOST}:${API_PORT}"

SITE_ID="${1:-}"
EXTRA_ARGS=()
if [[ -n "$SITE_ID" ]]; then
  EXTRA_ARGS+=(--site-id="$SITE_ID")
fi

if ! curl -sf "${WEB_SCRAPER_API}/api/sites" >/dev/null 2>&1; then
  echo "警告: web_scraper API 未响应 (${WEB_SCRAPER_API})" >&2
  echo "请先启动 Web UI，例如:" >&2
  echo "  WEB_UI_PORT=${API_PORT} python scripts/run_web_ui.py --port ${API_PORT}" >&2
  echo "或: bash scripts/start_all.sh" >&2
  echo "" >&2
fi

CLIENT_DIR="${ROOT}/client"
if [[ ! -d "${CLIENT_DIR}/node_modules/electron/dist" ]]; then
  echo "首次运行，安装 Electron 依赖…"
  # GitHub 直连易超时，国内可用 npmmirror 镜像
  export ELECTRON_MIRROR="${ELECTRON_MIRROR:-https://npmmirror.com/mirrors/electron/}"
  (cd "$CLIENT_DIR" && npm install)
fi

echo "规则配置客户端 · API ${WEB_SCRAPER_API}"
(cd "$CLIENT_DIR" && npm start -- "${EXTRA_ARGS[@]}")
