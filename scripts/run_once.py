#!/usr/bin/env python3
"""单次抓取指定站点（不启动调度器），用于调试适配器。"""

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
from src.core.schedule_eligibility import site_eligible_for_schedule
from src.core.scheduler import get_adapter_class, load_yaml
from src.db.mongo_repository import create_mongo_repository

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_once")

SITES_PATH = ROOT / "config" / "sites.yaml"
SCHEDULE_PATH = ROOT / "config" / "schedule.yaml"


async def main() -> None:
    parser = argparse.ArgumentParser(description="单次抓取指定站点")
    parser.add_argument("site_id", help="sites.yaml 中的站点 ID，如 ccgp_national")
    parser.add_argument("--max-items", type=int, default=10, help="最多抓取条数")
    parser.add_argument(
        "--with-detail",
        action="store_true",
        help="抓取详情页正文（覆盖 sites.yaml 中 fetch_detail 配置）",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="[已废弃] 等同于 --intelligent",
    )
    parser.add_argument(
        "--intelligent",
        action="store_true",
        help="启用智能爬取（语义分析→BFS→MongoDB）",
    )
    args = parser.parse_args()

    if not SITES_PATH.exists():
        logger.error("sites.yaml 不存在，请先运行: python scripts/generate_sites.py")
        sys.exit(1)

    sites_config = load_yaml(SITES_PATH)
    crawl_defaults = sites_config.get("crawl_defaults", {})
    sites_by_id = {s["id"]: s for s in sites_config.get("sites", [])}
    raw_site = sites_by_id.get(args.site_id)
    if not raw_site:
        logger.error("未找到站点: %s", args.site_id)
        sys.exit(1)
    site = {**crawl_defaults, **raw_site}

    adapter_name = site.get("adapter", "generic")
    if adapter_name == "generic" and not site_eligible_for_schedule(site):
        logger.error("站点 %s 尚未配置适配器（generic 需有效 crawl_rules.list_page）", args.site_id)
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
        fetch_detail = True if args.with_detail else None
        use_intelligent = args.intelligent or args.recursive or None
        result = await adapter.run(
            max_items=args.max_items,
            fetch_detail=fetch_detail,
            intelligent=use_intelligent,
            mongo_repo=mongo_repo,
        )
        if not result.success:
            logger.error("抓取失败: %s", result.error)
            sys.exit(1)

        processed = pipeline.process(result)
        pipeline.save(processed)

        print(f"\n=== {site['name']} ({args.site_id}) ===")
        print(
            f"抓取 {len(result.notices)} 条，新增 {len(processed.new)} 条，"
            f"正文补全 {len(processed.enriched)} 条"
        )
        display_notices = processed.all_for_storage or (
            result.notices if args.with_detail else []
        )
        for n in display_notices[:args.max_items]:
            date_str = n.publish_date.strftime("%Y-%m-%d") if n.publish_date else "-"
            print(f"  [{date_str}] {n.title[:60]}")
            print(f"           {n.url}")
            if n.content_text:
                preview = n.content_text[:80].replace("\n", " ")
                print(f"           正文: {preview}...")
            if n.purchaser:
                print(f"           采购人: {n.purchaser}")
            if n.budget:
                print(f"           预算: {n.budget}")
            if n.attachments:
                print(f"           附件: {len(n.attachments)} 个")
            if (args.intelligent or args.recursive) and n.depth:
                print(f"           depth={n.depth} type={n.page_type} parent={n.parent_url or '-'}")
    finally:
        await pool.stop()
        if mongo_repo:
            mongo_repo.close()


if __name__ == "__main__":
    asyncio.run(main())
