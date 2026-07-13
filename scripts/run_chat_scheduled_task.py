#!/usr/bin/env python3
"""手动执行对话定时任务（增量 sync + 飞书报告）。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_chat_scheduled_task")


async def main() -> None:
    parser = argparse.ArgumentParser(description="执行对话定时任务")
    parser.add_argument("task_id", help="chat_scheduled_tasks.json 中的任务 ID")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="sync 真实执行，飞书推送跳过（或未配置 webhook 时仅生成报告）",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="列出全部任务后退出",
    )
    args = parser.parse_args()

    from src.core.chat_scheduled_tasks import list_chat_scheduled_tasks, run_chat_scheduled_task_async
    from src.core.browser import BrowserPool
    from src.core.config import get_settings
    from src.core.pipeline import Pipeline
    from src.db.mongo_repository import create_mongo_repository

    if args.list:
        tasks = list_chat_scheduled_tasks()
        print(json.dumps(tasks, ensure_ascii=False, indent=2))
        return

    settings = get_settings()
    mongo_repo = create_mongo_repository(settings)
    pipeline = Pipeline(data_dir=settings.data_dir, mongo_repo=mongo_repo)
    browser_pool = BrowserPool()

    await browser_pool.start()
    try:
        result = await run_chat_scheduled_task_async(
            args.task_id,
            browser_pool=browser_pool,
            pipeline=pipeline,
            dry_run=args.dry_run,
        )
    finally:
        await browser_pool.stop()

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not result.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
