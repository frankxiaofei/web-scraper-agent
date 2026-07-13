#!/usr/bin/env bash
# 基础 CI：语法检查 + 可选 pytest
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> py_compile"
find src scripts -name '*.py' -print0 | while IFS= read -r -d '' f; do
  python -m py_compile "$f"
done
echo "py_compile OK"

if command -v pytest >/dev/null 2>&1 && [[ -d tests ]]; then
  echo "==> pytest"
  pytest -q
else
  echo "pytest 跳过（无 tests/ 或未安装 pytest）"
fi

echo "CI 通过"
