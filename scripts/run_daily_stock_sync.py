#!/usr/bin/env python3
"""每日股票同步入口（新闻 + 洞察缓存；scheduler 05:00 / 手动）。

与 run_daily_stock_news_sync.py 等价，供 script_cron 白名单使用。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml

from src.core.daily_stock_news_sync import run_daily_stock_news_sync, setup_daily_stock_log_file

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("run_daily_stock_sync")


def _load_schedule_config() -> dict:
    path = ROOT / "config" / "schedule.yaml"
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


async def main() -> None:
    setup_daily_stock_log_file()
    schedule_config = _load_schedule_config()
    summary = run_daily_stock_news_sync(schedule_config=schedule_config)
    logger.info("完成: %s", summary)
    if not summary.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
