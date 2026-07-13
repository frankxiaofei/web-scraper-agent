#!/usr/bin/env python3
"""启动股票领域分析 Web UI（独立端口，默认 8092）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_PORT = 8092
PORT_FILE = ROOT / "data" / ".stock_ui_port"


def _write_port(port: int) -> None:
    PORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    PORT_FILE.write_text(str(port), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="股票领域分析 Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = parser.parse_args()

    _write_port(args.port)

    import uvicorn

    print(f"股票领域分析: http://{args.host}:{args.port}/")
    print(f"主 Web UI（8090）: http://127.0.0.1:8090/ — 爬取/Hermes/任务管理")
    print(f"商机洞察专题（8091）: http://127.0.0.1:8091/")
    print("数据源: bid_notices 关键词筛选 + mock 演示（需配置专用股票数据源）")
    uvicorn.run(
        "src.web.stock_app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(ROOT / "src" / "web")] if args.reload else None,
    )


if __name__ == "__main__":
    main()
