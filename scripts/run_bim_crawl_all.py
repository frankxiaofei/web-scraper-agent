#!/usr/bin/env python3
"""国央企 enabled 站全量爬取 + BIM 分类 + 统计汇总。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backfill_bim_classify import run_backfill
from src.core.bim_classifier import is_bim_classify_enabled
from src.core.browser import BrowserPool
from src.core.config import get_settings
from src.core.daily_bim_sync import setup_daily_bim_log_file
from src.core.pipeline import Pipeline
from src.core.scheduler import load_yaml
from src.core.site_sync import load_enabled_sites, sync_site
from src.db.mongo_repository import create_mongo_repository

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_bim_crawl_all")

SCHEDULE_PATH = ROOT / "config" / "schedule.yaml"


def load_soe_enabled_sites() -> list[dict[str, Any]]:
    return load_enabled_sites()


def soe_bim_stats(site_ids: list[str]) -> dict[str, Any]:
    settings = get_settings()
    repo = create_mongo_repository(
        uri=settings.mongodb_uri,
        db_name=settings.mongodb_db,
        collection_name=settings.mongodb_collection,
    )
    if not repo or not repo.available or repo._notices is None:
        return {"available": False}

    col = repo._notices
    mvp_q = {
        "$or": [
            {"source_site_id": {"$in": site_ids}},
            {"source": {"$in": site_ids}},
            {"site_id": {"$in": site_ids}},
        ]
    }
    stats = {
        "available": True,
        "total": col.count_documents(mvp_q),
        "bim_true": col.count_documents({**mvp_q, "is_bim_related": True}),
        "bim_false": col.count_documents({**mvp_q, "is_bim_related": False}),
        "bim_unclassified": col.count_documents(
            {
                "$and": [
                    mvp_q,
                    {
                        "$or": [
                            {"is_bim_related": {"$exists": False}},
                            {"is_bim_related": None},
                        ]
                    },
                ]
            }
        ),
        "by_site": {},
    }
    for sid in site_ids:
        q = {"$or": [{"source_site_id": sid}, {"source": sid}, {"site_id": sid}]}
        stats["by_site"][sid] = {
            "total": col.count_documents(q),
            "bim_true": col.count_documents({**q, "is_bim_related": True}),
        }
    repo.close()
    return stats


async def crawl_soe_sites(
    sites: list[dict[str, Any]],
    *,
    max_items: int,
    stagger: int,
    site_id: str | None,
) -> dict[str, Any]:
    if site_id:
        sites = [s for s in sites if s.get("id") == site_id]
        if not sites:
            return {"success": False, "error": f"未找到国央企站点: {site_id}"}

    pool = BrowserPool(headless=True)
    pipeline = Pipeline()
    await pool.start()

    site_stats: list[dict[str, Any]] = []
    ok, fail = 0, 0
    total_notices = 0
    total_bim = 0

    try:
        for site in sites:
            sid = site.get("id", "")
            logger.info("开始爬取国央企站点: %s", sid)
            outcome = await sync_site(
                site,
                browser_pool=pool,
                pipeline=pipeline,
                max_items=max_items,
                update_status=False,
                trigger="daily_bim_sync",
            )
            notices = int(outcome.get("notices_count") or 0)
            bim_count = int(outcome.get("bim_count") or 0)
            stat = {
                "site_id": sid,
                "site_name": site.get("name") or sid,
                "success": bool(outcome.get("success")),
                "notices_count": notices,
                "new_count": int(outcome.get("new_count") or 0),
                "bim_count": bim_count,
                "duration_seconds": int(outcome.get("duration_seconds") or 0),
                "run_id": outcome.get("run_id"),
                "error": outcome.get("error"),
            }
            site_stats.append(stat)
            total_notices += notices
            total_bim += bim_count
            if outcome.get("success"):
                ok += 1
            else:
                fail += 1
            if stagger > 0 and site != sites[-1]:
                await asyncio.sleep(stagger)
    finally:
        await pool.stop()

    return {
        "success": fail == 0,
        "sites_ok": ok,
        "sites_fail": fail,
        "total_notices": total_notices,
        "total_bim": total_bim,
        "sites": site_stats,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="国央企 enabled 站 BIM 全量爬取与分类")
    parser.add_argument("--max-items", type=int, default=100, help="每站最多抓取条数")
    parser.add_argument("--stagger", type=int, default=30, help="站点间等待秒数")
    parser.add_argument("--site-id", type=str, default=None, help="仅爬取指定国央企站点")
    parser.add_argument(
        "--skip-crawl",
        action="store_true",
        help="跳过爬取，仅执行 backfill 与统计",
    )
    parser.add_argument(
        "--skip-backfill",
        action="store_true",
        help="跳过 backfill，仅爬取",
    )
    parser.add_argument(
        "--backfill-limit",
        type=int,
        default=300,
        help="backfill 最多处理条数",
    )
    args = parser.parse_args()

    log_path = setup_daily_bim_log_file()
    logger.info("BIM 全量任务日志: %s", log_path)

    sites = load_soe_enabled_sites()
    site_ids = [s["id"] for s in sites]
    logger.info("国央企站点 (%d): %s", len(site_ids), ", ".join(site_ids))

    before = soe_bim_stats(site_ids)
    logger.info(
        "分类前国央企统计: total=%s bim=%s false=%s unclassified=%s",
        before.get("total"),
        before.get("bim_true"),
        before.get("bim_false"),
        before.get("bim_unclassified"),
    )

    schedule_config = load_yaml(SCHEDULE_PATH) if SCHEDULE_PATH.exists() else {}
    scheduler_cfg = schedule_config.get("scheduler", {})
    stagger = args.stagger if args.stagger is not None else int(
        scheduler_cfg.get("daily_bim_sync_stagger_seconds", 30)
    )

    crawl_summary: dict[str, Any] = {"skipped": True}
    if not args.skip_crawl:
        crawl_summary = await crawl_soe_sites(
            sites,
            max_items=args.max_items,
            stagger=stagger,
            site_id=args.site_id,
        )
        logger.info("爬取汇总: %s", json.dumps(crawl_summary, ensure_ascii=False, indent=2))

    backfill_summary: dict[str, int] = {"attempted": 0, "updated": 0, "skipped": 0}
    if not args.skip_backfill:
        backfill_summary = run_backfill(
            source=args.site_id,
            limit=args.backfill_limit,
            force=False,
        )
        logger.info(
            "Backfill 完成: 处理 %d 条，更新 %d 条",
            backfill_summary["attempted"],
            backfill_summary["updated"],
        )

    after = soe_bim_stats(site_ids)
    summary = {
        "success": crawl_summary.get("success", True) and after.get("available", False),
        "bim_classify_llm": is_bim_classify_enabled(),
        "soe_sites": site_ids,
        "mvp_sites": site_ids,
        "before": before,
        "after": after,
        "crawl": crawl_summary,
        "backfill": backfill_summary,
        "view_url": "http://127.0.0.1:8090/?bim_only=true",
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info("最终汇总:\n%s", json.dumps(summary, ensure_ascii=False, indent=2))

    if not summary.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
