#!/usr/bin/env python3
"""执行已生成的爬取脚本/规则（无 LLM，供 script_cron 白名单调度）。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.crawl_script_service import run_generated_crawl

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_generated_crawl")


async def main() -> None:
    parser = argparse.ArgumentParser(description="执行已生成的站点爬取脚本")
    parser.add_argument("site_id", help="站点 ID")
    parser.add_argument("--dry-run", action="store_true", help="试跑，不入库")
    parser.add_argument("--max-items", type=int, default=None, help="最多抓取条数")
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="不入库（等同 dry-run 对 YAML 规则）",
    )
    args = parser.parse_args()

    dry_run = args.dry_run or args.no_persist
    result = await run_generated_crawl(
        args.site_id,
        dry_run=dry_run,
        max_items=args.max_items,
        persist=not dry_run,
        trigger="cli",
    )
    if not result.get("ok"):
        logger.error("执行失败: %s", result.get("error") or result.get("errors"))
        sys.exit(1)

    mode = result.get("mode", "")
    if mode == "sync":
        logger.info(
            "完成 site=%s notices=%s new=%s",
            args.site_id,
            result.get("notices_count"),
            result.get("new_count"),
        )
    elif mode == "dry_run":
        logger.info("试跑完成 site=%s items=%d", args.site_id, len(result.get("items") or []))
    else:
        logger.info("执行完成 site=%s mode=%s", args.site_id, mode)


if __name__ == "__main__":
    asyncio.run(main())
