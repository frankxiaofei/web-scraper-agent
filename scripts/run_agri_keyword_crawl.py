#!/usr/bin/env python3
"""智慧农业关键词爬取：对 agri 专用 7 站按关键词搜索/列表抓取。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.daily_agri_sync import run_daily_agri_sync, setup_daily_agri_log_file
from src.core.scheduler import load_yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_agri_keyword_crawl")

SCHEDULE_PATH = ROOT / "config" / "schedule.yaml"


async def main() -> None:
    parser = argparse.ArgumentParser(description="智慧农业 7 站关键词爬取同步")
    parser.add_argument("--max-items", type=int, default=None, help="每站/每关键词最多抓取条数")
    parser.add_argument("--stagger", type=int, default=None, help="站点/关键词间等待秒数")
    parser.add_argument("--site-id", type=str, default=None, help="仅同步指定 agri 站点")
    parser.add_argument(
        "--no-keyword-search",
        action="store_true",
        help="禁用站点内关键词搜索，仅抓默认列表",
    )
    args = parser.parse_args()

    log_path = setup_daily_agri_log_file()
    logger.info("农业关键词爬取日志: %s", log_path)

    schedule_config = load_yaml(SCHEDULE_PATH) if SCHEDULE_PATH.exists() else {}
    summary = await run_daily_agri_sync(
        schedule_config=schedule_config,
        max_items=args.max_items,
        stagger=args.stagger,
        site_id=args.site_id,
        update_status=True,
        use_keyword_search=not args.no_keyword_search,
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
