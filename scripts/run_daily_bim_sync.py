#!/usr/bin/env python3
"""每日 BIM 招标同步：全站 intelligent + with-detail 爬取并自动 BIM 分类。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.daily_bim_sync import run_daily_bim_sync, setup_daily_bim_log_file
from src.core.scheduler import load_yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_daily_bim_sync")

SCHEDULE_PATH = ROOT / "config" / "schedule.yaml"


async def main() -> None:
    parser = argparse.ArgumentParser(description="每日 BIM 招标全站同步")
    parser.add_argument("--max-items", type=int, default=None, help="每站最多抓取条数")
    parser.add_argument("--stagger", type=int, default=None, help="站点间等待秒数")
    parser.add_argument("--site-id", type=str, default=None, help="仅同步指定站点（dry-run）")
    parser.add_argument("--dry-run", action="store_true", help="同 --site-id，默认 zycg_national")
    args = parser.parse_args()

    site_id = args.site_id
    if args.dry_run and not site_id:
        site_id = "zycg_national"

    log_path = setup_daily_bim_log_file()
    logger.info("BIM 同步日志: %s", log_path)

    schedule_config = load_yaml(SCHEDULE_PATH) if SCHEDULE_PATH.exists() else {}
    summary = await run_daily_bim_sync(
        schedule_config=schedule_config,
        max_items=args.max_items,
        stagger=args.stagger,
        site_id=site_id,
        update_status=not bool(site_id),
    )

    logger.info("汇总: %s", json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
