#!/usr/bin/env bash
# 打印 Chrome 爬取工作流录制插件安装路径，macOS 下可选打开 chrome://extensions
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXT_PATH="$ROOT/extensions/crawl-workflow-recorder"

echo "爬取工作流录制 Chrome 插件"
echo "================================"
echo "扩展目录（Load unpacked）："
echo "  $EXT_PATH"
echo ""
echo "安装步骤："
echo "  1. 打开 chrome://extensions"
echo "  2. 开启「开发者模式」"
echo "  3. 点击「加载已解压的扩展程序」"
echo "  4. 选择上述目录"
echo ""
echo "然后在 Web UI 工作流页 →「浏览器录制」标签 →「开始录制」"
echo ""

if [[ "$(uname -s)" == "Darwin" ]]; then
  read -r -p "是否在 Chrome 中打开扩展管理页？(y/N) " ans
  if [[ "${ans,,}" == "y" ]]; then
    open -a "Google Chrome" "chrome://extensions" 2>/dev/null || open "chrome://extensions" 2>/dev/null || true
  fi
fi
