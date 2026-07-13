#!/usr/bin/env bash
# 从本机 rsync 同步 web_scraper 到远程并执行 setup_stack.sh
#
# 用法:
#   export REMOTE_HOST=10.x.x.x REMOTE_USER=li_xf10 REMOTE_PORT=22122
#   export SSHPASS='...'   # 或配置 SSH key
#   bash scripts/deploy_remote.sh
#   bash scripts/deploy_remote.sh --with-env    # 同步本地 .env（含密钥，勿提交 git）
#   bash scripts/deploy_remote.sh --setup-only  # 仅远程 setup，不同步代码
#   bash scripts/deploy_remote.sh --native      # 基础设施 Docker + 宿主机 screen（推荐内网机）
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_HOST="${REMOTE_HOST:?请设置 REMOTE_HOST}"
REMOTE_USER="${REMOTE_USER:?请设置 REMOTE_USER}"
REMOTE_PORT="${REMOTE_PORT:-22}"
REMOTE_DIR="${REMOTE_DIR:-/home/${REMOTE_USER}/web_scraper}"
WITH_ENV=false
SETUP_ONLY=false
NATIVE=false

for arg in "$@"; do
  case "$arg" in
    --with-env) WITH_ENV=true ;;
    --setup-only) SETUP_ONLY=true ;;
    --native) NATIVE=true ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *) echo "未知参数: $arg" >&2; exit 1 ;;
  esac
done

SSH_OPTS=(-o StrictHostKeyChecking=no -p "$REMOTE_PORT")
RSYNC_SSH="ssh ${SSH_OPTS[*]}"
REMOTE="${REMOTE_USER}@${REMOTE_HOST}"

ssh_cmd() {
  if [ -n "${SSHPASS:-}" ] && command -v sshpass >/dev/null 2>&1; then
    sshpass -e ssh "${SSH_OPTS[@]}" "$REMOTE" "$@"
  else
    ssh "${SSH_OPTS[@]}" "$REMOTE" "$@"
  fi
}

rsync_cmd() {
  if [ -n "${SSHPASS:-}" ] && command -v sshpass >/dev/null 2>&1; then
    sshpass -e rsync -az --delete -e "ssh ${SSH_OPTS[*]}" "$@"
  else
    rsync -az --delete -e "ssh ${SSH_OPTS[*]}" "$@"
  fi
}

echo ">>> 远程: ${REMOTE}:${REMOTE_DIR}"

if [ "$SETUP_ONLY" = false ]; then
  ssh_cmd "mkdir -p '$REMOTE_DIR'"
  echo ">>> rsync 项目代码..."
  rsync_cmd \
    --exclude '.git/' \
    --exclude '.venv/' \
    --exclude '__pycache__/' \
    --exclude '.pytest_cache/' \
    --exclude 'logs/' \
    --exclude 'data/hermes-agent/' \
    --exclude 'data/notices.jsonl' \
    --exclude 'data/*.log' \
    --exclude 'data/crawl_agent_chats/' \
    --exclude 'docs/chrome-mac-arm64/' \
    --exclude 'docs/ms-playwright/' \
    --exclude '.env' \
    "$ROOT/" "$REMOTE:$REMOTE_DIR/"

  if [ "$WITH_ENV" = true ] && [ -f "$ROOT/.env" ]; then
    echo ">>> 同步本地 .env（含密钥）"
    rsync_cmd "$ROOT/.env" "$REMOTE:$REMOTE_DIR/.env"
  elif ! ssh_cmd "test -f '$REMOTE_DIR/.env'"; then
    echo ">>> 远程无 .env，上传 .env.remote.example 供参考"
    rsync_cmd "$ROOT/.env.remote.example" "$REMOTE:$REMOTE_DIR/.env.remote.example"
  fi
fi

echo ">>> 远程执行 setup"
if [ "$NATIVE" = true ]; then
  ssh_cmd "chmod +x '$REMOTE_DIR/scripts/remote/'*.sh && cd '$REMOTE_DIR' && \
    sudo docker-compose -f docker/docker-compose.yml -f docker/docker-compose.remote.yml up -d postgres redis mongo 2>/dev/null || true; \
    bash scripts/remote/setup_native.sh"
else
  ssh_cmd "chmod +x '$REMOTE_DIR/scripts/remote/'*.sh && cd '$REMOTE_DIR' && bash scripts/remote/setup_stack.sh"
fi

echo ">>> 部署完成: http://${REMOTE_HOST}:8090/"
