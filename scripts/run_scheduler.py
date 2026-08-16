#!/usr/bin/env python3
"""启动定时爬虫调度器。"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.browser import BrowserPool
from src.core.pipeline import Pipeline
from src.core.scheduler import ScraperScheduler, load_yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_scheduler")

SITES_PATH = ROOT / "config" / "sites.yaml"
SCHEDULE_PATH = ROOT / "config" / "schedule.yaml"


async def main() -> None:
    if not SITES_PATH.exists():
        logger.error("sites.yaml 不存在，请先运行: python scripts/generate_sites.py")
        sys.exit(1)

    sites_config = load_yaml(SITES_PATH)
    schedule_config = load_yaml(SCHEDULE_PATH)

    scheduler_cfg = schedule_config.get("scheduler", {})
    pool_size = scheduler_cfg.get("max_workers", 2)

    browser_cfg = schedule_config.get("browser", {})
    executable_path = browser_cfg.get("executable_path")
    if executable_path and not Path(executable_path).is_absolute():
        executable_path = str(ROOT / executable_path)

    browser_pool = BrowserPool(
        headless=True,
        pool_size=pool_size,
        executable_path=executable_path,
    )
    pipeline = Pipeline()
    scheduler = ScraperScheduler(
        sites_config=sites_config,
        schedule_config=schedule_config,
        browser_pool=browser_pool,
        pipeline=pipeline,
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("收到停止信号，正在关闭...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            signal.signal(sig, lambda _s, _f: loop.call_soon_threadsafe(_signal_handler))

    await scheduler.start(run_on_start=True)
    logger.info("调度器已启动，等待任务执行...")

    await stop_event.wait()
    await scheduler.shutdown()
    logger.info("调度器已停止")


if __name__ == "__main__":
    asyncio.run(main())
