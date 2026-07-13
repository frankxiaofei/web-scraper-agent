#!/usr/bin/env bash
# 强制停止本地招标栈（screen + 孤儿进程 + 8090/8080/8642 端口）。
# 默认不操作 Docker hermes-agent 容器（profile docker-hermes-agent）；容器模式见 make stop-hermes-agent-docker。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

stop_screen() {
	local name="$1"
	if screen -list 2>/dev/null | grep -q "\.${name}[[:space:]]"; then
		screen -S "$name" -X quit 2>/dev/null || true
		echo "screen: $name 已退出"
	fi
}

kill_port() {
	local port="$1"
	if ! command -v lsof >/dev/null 2>&1; then
		return 0
	fi
	local pids
	pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
	if [ -n "$pids" ]; then
		echo "端口 $port: 终止 PID $pids"
		kill $pids 2>/dev/null || kill -9 $pids 2>/dev/null || true
	fi
}

echo "==> xkill: 本地 screen 会话"
for s in web_scraper_ui web_scraper_bim_ui web_scraper_sched web_scraper_hermes_agent; do
	stop_screen "$s"
done

echo "==> xkill: 匹配进程"
pkill -f '[.]venv/bin/python.*scripts/run_web_ui.py' 2>/dev/null || true
pkill -f '[p]ython.*scripts/run_web_ui.py' 2>/dev/null || true
pkill -f '[.]venv/bin/python.*scripts/run_bim_ui.py' 2>/dev/null || true
pkill -f '[p]ython.*scripts/run_bim_ui.py' 2>/dev/null || true
pkill -f '[.]venv/bin/python.*scripts/run_scheduler.py' 2>/dev/null || true
pkill -f '[p]ython.*scripts/run_scheduler.py' 2>/dev/null || true
pkill -f '[.]venv/bin/python.*crawl_dispatch_server.py' 2>/dev/null || true
pkill -f '[.]venv/bin/hermes gateway run' 2>/dev/null || true
pkill -f '[h]ermes gateway run' 2>/dev/null || true
pkill -f '[s]tart-local.sh' 2>/dev/null || true

echo "==> xkill: 端口 8090 / 8091 / 8080 / 8642（本地进程，非 Docker）"
for p in 8090 8091 8080 8642; do
	kill_port "$p"
done

echo "xkill 完成（未 stop Docker hermes-agent 容器；容器模式: make stop-hermes-agent-docker）"
