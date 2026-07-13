#!/usr/bin/env python3
"""一次性执行每日全站同步（串行错峰，供 cron 或手动调用）。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.browser import BrowserPool
from src.core.pipeline import Pipeline
from src.core.scheduler import load_yaml
from src.core.schedule_eligibility import site_eligible_for_schedule
from src.core.site_sync import load_enabled_sites, sync_site
from src.core.sync_status import get_sync_status_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_daily_sync")

SCHEDULE_PATH = ROOT / "config" / "schedule.yaml"


async def main() -> None:
    parser = argparse.ArgumentParser(description="串行同步所有 enabled 站点")
    parser.add_argument("--max-items", type=int, default=None, help="每站最多抓取条数")
    parser.add_argument("--stagger", type=int, default=None, help="站点间等待秒数")
    args = parser.parse_args()

    schedule_config = load_yaml(SCHEDULE_PATH) if SCHEDULE_PATH.exists() else {}
    scheduler_cfg = schedule_config.get("scheduler", {})
    stagger = args.stagger if args.stagger is not None else int(
        scheduler_cfg.get("daily_sync_stagger_seconds", 30)
    )
    max_items = args.max_items if args.max_items is not None else int(
        scheduler_cfg.get("daily_sync_max_items", 10)
    )
    cron = scheduler_cfg.get("daily_sync_cron", "0 2 * * *")

    sites = load_enabled_sites()
    if not sites:
        logger.error("没有 enabled 站点")
        sys.exit(1)

    status_store = get_sync_status_store()
    status_store.set_schedule_meta(cron=cron)

    pool = BrowserPool(headless=True)
    pipeline = Pipeline()
    await pool.start()

    ok, fail = 0, 0
    try:
        for site in sites:
            site_id = site.get("id", "")
            if not site_eligible_for_schedule(site):
                if site.get("adapter") == "generic":
                    status_store.ensure_site(
                        site_id, site.get("name") or site_id, site.get("url") or ""
                    )
                    logger.info("跳过 generic 站点（无 crawl_rules）: %s", site_id)
                continue
            logger.info("同步站点: %s", site_id)
            outcome = await sync_site(
                site,
                browser_pool=pool,
                pipeline=pipeline,
                max_items=max_items,
                update_status=True,
            )
            if outcome.get("success"):
                ok += 1
            else:
                fail += 1
            if stagger > 0:
                await asyncio.sleep(stagger)
    finally:
        await pool.stop()

    logger.info("每日同步完成: 成功 %d, 失败 %d", ok, fail)
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    asyncio.run(main())
