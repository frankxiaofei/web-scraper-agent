#!/usr/bin/env python3
"""每日商机线索同步：P0/P1 站点 sync + 商机分析 + 简报缓存。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.biz_clue_sync import run_daily_biz_clue_sync, setup_daily_biz_clue_log_file
from src.core.scheduler import load_yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_daily_biz_clue_sync")

SCHEDULE_PATH = ROOT / "config" / "schedule.yaml"


async def main() -> None:
    parser = argparse.ArgumentParser(description="每日商机线索同步")
    parser.add_argument("--max-items", type=int, default=None, help="每站最多抓取条数")
    parser.add_argument("--stagger", type=int, default=None, help="站点间等待秒数")
    parser.add_argument("--site-id", type=str, default=None, help="仅同步指定站点")
    parser.add_argument("--no-analysis", action="store_true", help="跳过商机分析")
    args = parser.parse_args()

    log_path = setup_daily_biz_clue_log_file()
    logger.info("商机同步日志: %s", log_path)

    schedule_config = load_yaml(SCHEDULE_PATH) if SCHEDULE_PATH.exists() else {}
    summary = await run_daily_biz_clue_sync(
        schedule_config=schedule_config,
        max_items=args.max_items,
        stagger=args.stagger,
        site_id=args.site_id,
        update_status=not bool(args.site_id),
        trigger_analysis=not args.no_analysis,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
