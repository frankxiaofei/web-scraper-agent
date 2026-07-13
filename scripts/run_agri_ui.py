#!/usr/bin/env python3
"""启动商机洞察 Web UI（独立端口，默认 8091）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_PORT = 8091
PORT_FILE = ROOT / "data" / ".agri_ui_port"


def _write_port(port: int) -> None:
    PORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    PORT_FILE.write_text(str(port), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="商机洞察 Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = parser.parse_args()

    _write_port(args.port)

    import uvicorn

    print(f"商机洞察: http://{args.host}:{args.port}/")
    print(f"主 Web UI（8090）: http://127.0.0.1:8090/ — 爬取/Hermes/任务管理")
    print("数据源: MongoDB（可用时）→ 降级 data/notices.jsonl")
    uvicorn.run(
        "src.web.agri_app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(ROOT / "src" / "web")] if args.reload else None,
    )


if __name__ == "__main__":
    main()
