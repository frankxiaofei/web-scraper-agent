#!/usr/bin/env python3
"""健康检查：PostgreSQL / Redis / docs Chrome / 最近 scrape_jobs 状态。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.alert import get_alert_manager
from src.core.browser import BrowserPool, default_chromium_executable
from src.core.config import get_settings
from src.core.dedup import create_dedup_store
from src.db.repository import ScrapeJobRepository
from src.db.session import check_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("health_check")


def check_postgres(database_url: str | None) -> tuple[bool, str]:
    if not database_url:
        return False, "未配置 DATABASE_URL"
    if check_connection(database_url):
        return True, "连接正常"
    return False, "连接失败"


def check_redis(redis_url: str | None) -> tuple[bool, str]:
    if not redis_url:
        return False, "未配置 REDIS_URL"
    store = create_dedup_store(redis_url=redis_url)
    if getattr(store, "available", False):
        return True, "连接正常"
    return False, "连接失败或不可用"


async def check_chrome() -> tuple[bool, str]:
    exe = default_chromium_executable()
    if not exe:
        return False, "未找到 docs/ Chrome 可执行文件"
    pool = BrowserPool(headless=True, executable_path=exe)
    try:
        await pool.start()
        async with pool.new_page() as page:
            await page.goto("about:blank", wait_until="domcontentloaded", timeout=15000)
        return True, f"可启动 ({Path(exe).name})"
    except Exception as e:
        return False, str(e)
    finally:
        await pool.stop()


def check_scrape_jobs(database_url: str | None, limit: int = 10) -> tuple[bool, str, list[dict]]:
    if not database_url or not check_connection(database_url):
        return False, "数据库不可用，无法查询 scrape_jobs", []
    try:
        repo = ScrapeJobRepository(database_url)
        jobs = repo.get_recent_jobs(limit=limit)
    except Exception as e:
        return False, f"查询失败: {e}", []

    if not jobs:
        return True, "无历史任务记录", []

    success = sum(1 for j in jobs if j["status"] == "success")
    failure = len(jobs) - success
    summary = f"最近 {len(jobs)} 条: 成功 {success}, 失败 {failure}"
    return True, summary, jobs


def main() -> int:
    parser = argparse.ArgumentParser(description="爬虫系统健康检查")
    parser.add_argument("--jobs-limit", type=int, default=10, help="查询 scrape_jobs 条数")
    parser.add_argument("--alert-on-failure-rate", action="store_true", help="失败率超阈值时触发告警")
    args = parser.parse_args()

    settings = get_settings()
    checks: list[tuple[str, bool, str]] = []

    ok, msg = check_postgres(settings.database_url)
    checks.append(("PostgreSQL", ok, msg))

    ok, msg = check_redis(settings.redis_url)
    checks.append(("Redis", ok, msg))

    chrome_ok, chrome_msg = asyncio.run(check_chrome())
    checks.append(("Chrome", chrome_ok, chrome_msg))

    jobs_ok, jobs_msg, jobs = check_scrape_jobs(settings.database_url, args.jobs_limit)
    checks.append(("scrape_jobs", jobs_ok, jobs_msg))

    print("\n=== 健康检查 ===")
    all_required_ok = True
    for name, passed, detail in checks:
        status = "OK" if passed else "FAIL"
        print(f"  [{status}] {name}: {detail}")
        if name in ("PostgreSQL", "Redis", "Chrome") and not passed:
            all_required_ok = False

    if jobs and args.alert_on_failure_rate:
        scheduler_threshold = 0.5
        get_alert_manager().check_failure_rate(jobs, threshold=scheduler_threshold)

    if jobs:
        print("\n最近任务:")
        for j in jobs[:5]:
            print(
                f"  {j.get('started_at', '-')} | {j['site_id']} | "
                f"{j['status']} | notices={j.get('notices_count', 0)}"
            )

    print()
    return 0 if all_required_ok else 1


if __name__ == "__main__":
    sys.exit(main())
