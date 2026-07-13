#!/usr/bin/env bash
# 使用国内镜像安装项目依赖（清华 PyPI + npmmirror Playwright）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# 清华 PyPI 镜像（备选：阿里云 https://mirrors.aliyun.com/pypi/simple/）
PIP_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
PIP_TRUSTED_HOST="pypi.tuna.tsinghua.edu.cn"

# Playwright Chromium 国内下载镜像
export PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright"

cd "$PROJECT_ROOT"

echo ">>> 使用清华源安装 Python 依赖..."
pip install -i "$PIP_INDEX" --trusted-host "$PIP_TRUSTED_HOST" -r requirements.txt

# 避免指向错误/空的自定义浏览器目录
unset PLAYWRIGHT_BROWSERS_PATH

playwright_chromium_ready() {
  python3 << 'PY'
from playwright.sync_api import sync_playwright

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        browser.close()
except Exception as e:
    raise SystemExit(f"Chromium 不可用: {e}") from e
PY
}

playwright_headless_smoke_test() {
  echo ">>> 运行 Playwright Chromium 最小 headless 测试..."
  if ! playwright_chromium_ready; then
    echo ">>> 错误: Playwright Chromium headless 测试失败" >&2
    exit 1
  fi
  echo ">>> Playwright Chromium headless 测试通过"
}

echo ">>> 检测 Playwright Chromium 是否已安装..."
if playwright_chromium_ready 2>/dev/null; then
  echo ">>> Chromium 已存在，跳过下载"
  playwright_headless_smoke_test
  echo ">>> 依赖安装完成"
  exit 0
fi

echo ">>> 安装 Playwright Chromium（优先国内镜像）..."
if ! python3 -m playwright install chromium; then
  echo ">>> 国内镜像安装失败，尝试官方源..."
  unset PLAYWRIGHT_DOWNLOAD_HOST
  python3 -m playwright install chromium
fi

playwright_headless_smoke_test

echo ">>> 依赖安装完成"
