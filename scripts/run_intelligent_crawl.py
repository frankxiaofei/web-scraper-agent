#!/usr/bin/env python3
"""智能爬取指定站点（独立入口，直连 Orchestrator + MongoDB）。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.browser import BrowserPool
from src.core.config import get_settings
from src.core.pipeline import Pipeline
from src.core.scheduler import get_adapter_class, load_yaml
from src.db.mongo_repository import create_mongo_repository
from src.intelligent_crawl.orchestrator import IntelligentCrawlOrchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_intelligent_crawl")

SITES_PATH = ROOT / "config" / "sites.yaml"
SCHEDULE_PATH = ROOT / "config" / "schedule.yaml"


async def main() -> None:
    parser = argparse.ArgumentParser(description="智能爬取指定站点")
    parser.add_argument("site_id", help="sites.yaml 中的站点 ID，如 ccgp_national")
    parser.add_argument("--max-pages", type=int, default=50, help="最多爬取页面数")
    parser.add_argument("--max-depth", type=int, default=3, help="最大递归深度")
    parser.add_argument("--max-items", type=int, default=10, help="列表种子条数")
    parser.add_argument(
        "--with-detail",
        action="store_true",
        help="先抓取详情页正文再智能扩展",
    )
    args = parser.parse_args()

    if not SITES_PATH.exists():
        logger.error("sites.yaml 不存在")
        sys.exit(1)

    sites_config = load_yaml(SITES_PATH)
    crawl_defaults = sites_config.get("crawl_defaults", {})
    sites_by_id = {s["id"]: s for s in sites_config.get("sites", [])}
    raw_site = sites_by_id.get(args.site_id)
    if not raw_site:
        logger.error("未找到站点: %s", args.site_id)
        sys.exit(1)
    site = {**crawl_defaults, **raw_site}

    max_depth = args.max_depth or int(site.get("max_crawl_depth", crawl_defaults.get("max_crawl_depth", 3)))

    adapter_name = site.get("adapter", "generic")
    if adapter_name == "generic":
        logger.error("站点 %s 尚未配置适配器", args.site_id)
        sys.exit(1)

    settings = get_settings()
    mongo_repo = create_mongo_repository(
        uri=settings.mongodb_uri,
        db_name=settings.mongodb_db,
        collection_name=settings.mongodb_collection,
    )

    pool = BrowserPool(headless=True)
    pipeline = Pipeline(settings=settings)
    await pool.start()

    schedule_config = load_yaml(SCHEDULE_PATH) if SCHEDULE_PATH.exists() else {}
    scheduler_cfg = schedule_config.get("scheduler", {})
    schedule_defaults = {
        "default_min_delay_seconds": scheduler_cfg.get("default_min_delay_seconds", 0),
        "max_retries": scheduler_cfg.get("max_retries", 3),
    }

    try:
        adapter_cls = get_adapter_class(adapter_name)
        adapter = adapter_cls(site, pool)
        adapter.set_schedule_defaults(schedule_defaults)

        # 先抓列表种子
        fetch_detail = True if args.with_detail else bool(site.get("fetch_detail"))
        list_result = await adapter.run(
            max_items=args.max_items,
            fetch_detail=fetch_detail,
            intelligent=False,
        )
        if not list_result.success:
            logger.error("列表抓取失败: %s", list_result.error)
            sys.exit(1)

        orchestrator = IntelligentCrawlOrchestrator(
            browser=pool,
            site_config=site,
            adapter=adapter,
            max_depth=max_depth,
            max_pages=args.max_pages,
            min_delay=adapter._min_delay_seconds(),
            mongo_repo=mongo_repo,
            fetch_detail=fetch_detail,
        )
        all_notices = await orchestrator.run_from_seeds(
            list_result.notices,
            max_pages=args.max_pages,
        )

        from src.core.models import ScrapeResult

        result = ScrapeResult(
            site_id=args.site_id,
            success=True,
            notices=all_notices,
            duration_seconds=0,
        )
        processed = pipeline.process(result)
        pipeline.save(processed)

        depths = sorted({n.depth for n in all_notices})
        print(f"\n=== 智能爬取 {site['name']} ({args.site_id}) ===")
        print(
            f"共 {len(all_notices)} 条, 新增 {len(processed.new)} 条, "
            f"正文补全 {len(processed.enriched)} 条, depth 分布: {depths}"
        )
        for n in all_notices[:args.max_items]:
            print(f"  [d={n.depth}] {n.title[:60]}")
            print(f"           {n.url}")
            if n.parent_url:
                print(f"           parent={n.parent_url}")

        if mongo_repo and mongo_repo.available:
            docs = mongo_repo.find_by_source(args.site_id, limit=5)
            print(f"\nMongoDB 最近 {len(docs)} 条 (bid_notices)")
            for d in docs:
                print(f"  depth={d.get('depth')} type={d.get('page_type')} {d.get('title', '')[:40]}")
    finally:
        await pool.stop()
        if mongo_repo:
            mongo_repo.close()


if __name__ == "__main__":
    asyncio.run(main())
