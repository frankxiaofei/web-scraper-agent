#!/usr/bin/env python3
"""启动招标公告 Web UI。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description="招标公告 Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8080, help="端口（默认 8080）")
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = parser.parse_args()

    import uvicorn

    print(f"招标公告 Web UI: http://{args.host}:{args.port}/")
    print("数据源: MongoDB（可用时）→ 降级 data/notices.jsonl")
    uvicorn.run(
        "src.web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(ROOT / "src" / "web")] if args.reload else None,
    )


if __name__ == "__main__":
    main()
