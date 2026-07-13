#!/usr/bin/env bash
# 使用 docs/ 下本地 Chromium 运行爬虫命令。
# 用法: bash scripts/run_with_local_chrome.sh [command...]
# 示例: bash scripts/run_with_local_chrome.sh python scripts/run_scheduler.py

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_CHROME="${ROOT}/docs/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"

export PLAYWRIGHT_CHROMIUM_EXECUTABLE="${PLAYWRIGHT_CHROMIUM_EXECUTABLE:-${CHROMIUM_EXECUTABLE_PATH:-$DEFAULT_CHROME}}"

if [[ ! -x "$PLAYWRIGHT_CHROMIUM_EXECUTABLE" ]]; then
  echo "错误: Chromium 不可执行或不存在: $PLAYWRIGHT_CHROMIUM_EXECUTABLE" >&2
  exit 1
fi

cd "$ROOT"
if [[ $# -eq 0 ]]; then
  set -- python scripts/run_scheduler.py
fi

exec "$@"
