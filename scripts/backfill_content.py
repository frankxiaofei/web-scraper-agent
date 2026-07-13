#!/usr/bin/env python3
"""为 MongoDB 中缺少正文的公告补抓详情页。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.adapters.base import BaseAdapter
from src.core.browser import BrowserPool
from src.core.config import get_settings
from src.core.models import BidNotice, NoticeCategory
from src.core.scheduler import get_adapter_class, load_yaml
from src.db.mongo_repository import MongoRepository, create_mongo_repository

SITES_PATH = ROOT / "config" / "sites.yaml"
DATA_DIR = ROOT / "data"
TZ_CN = timezone(timedelta(hours=8))

logger = logging.getLogger("backfill_content")


def setup_logging(log_path: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def load_sites_config() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if not SITES_PATH.exists():
        raise FileNotFoundError("sites.yaml 不存在")
    sites_config = load_yaml(SITES_PATH)
    crawl_defaults = sites_config.get("crawl_defaults", {})
    sites_by_id = {
        s["id"]: {**crawl_defaults, **s} for s in sites_config.get("sites", [])
    }
    return crawl_defaults, sites_by_id


def adapter_supports_fetch_detail(adapter_name: str) -> bool:
    if adapter_name == "generic":
        return False
    cls = get_adapter_class(adapter_name)
    return cls.fetch_detail is not BaseAdapter.fetch_detail


def content_stats(mongo_repo: MongoRepository) -> dict[str, int]:
    if not mongo_repo.available or mongo_repo._notices is None:
        return {"total": 0, "with_content": 0, "without_content": 0}
    col = mongo_repo._notices
    missing_q = {
        "$and": [
            {
                "$or": [
                    {"content_text": {"$exists": False}},
                    {"content_text": None},
                    {"content_text": ""},
                ]
            },
            {
                "$or": [
                    {"content_html": {"$exists": False}},
                    {"content_html": None},
                    {"content_html": ""},
                ]
            },
        ]
    }
    total = col.count_documents({})
    without = col.count_documents(missing_q)
    return {"total": total, "without_content": without, "with_content": total - without}


def collect_backfill_docs(
    mongo_repo: MongoRepository,
    site_id: str,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    scan_docs = mongo_repo.find_missing_content(
        source=site_id, limit=limit * 20, detail_only=False
    )
    docs: list[dict[str, Any]] = []
    skipped = 0
    for doc in scan_docs:
        reason = MongoRepository.missing_content_skip_reason(doc)
        url = doc.get("url") or doc.get("detail_url") or ""
        if reason:
            skipped += 1
            logger.info("跳过补抓 (%s): %s", reason, url)
            continue
        docs.append(doc)
        if len(docs) >= limit:
            break
    return docs, skipped


async def backfill_site(
    site_id: str,
    site: dict[str, Any],
    mongo_repo: MongoRepository,
    pool: BrowserPool,
    *,
    limit_per_batch: int,
    until_empty: bool,
) -> dict[str, Any]:
    adapter_name = site.get("adapter", "generic")
    if not adapter_supports_fetch_detail(adapter_name):
        return {
            "site_id": site_id,
            "status": "skipped",
            "reason": f"适配器 {adapter_name} 未实现 fetch_detail",
            "updated": 0,
            "attempted": 0,
        }

    adapter_cls = get_adapter_class(adapter_name)
    adapter = adapter_cls(site, pool)
    total_updated = 0
    total_attempted = 0
    total_skipped_urls = 0
    rounds = 0

    while True:
        rounds += 1
        docs, skipped = collect_backfill_docs(mongo_repo, site_id, limit_per_batch)
        total_skipped_urls += skipped
        if not docs:
            break
        total_attempted += len(docs)
        batch_updated = 0
        for doc in docs:
            notice = BidNotice(
                title=doc.get("title") or "",
                url=doc.get("url") or doc.get("detail_url") or "",
                source_site_id=doc.get("source_site_id") or site_id,
                source_site_name=doc.get("source_site_name") or site.get("name", site_id),
                source_url=doc.get("source_url") or site.get("url", ""),
                content_hash=doc.get("content_hash"),
                category=NoticeCategory.TENDER,
                is_bim_related=doc.get("is_bim_related"),
                bim_confidence=doc.get("bim_confidence"),
                bim_reason=doc.get("bim_reason"),
                bim_tags=doc.get("bim_tags"),
                bim_classified_at=doc.get("bim_classified_at"),
            )
            if not notice.url:
                continue
            try:
                enriched = await adapter.fetch_detail(notice)
                from src.core.key_info_extractor import apply_key_info_to_notice

                enriched = apply_key_info_to_notice(enriched)
            except Exception as e:
                logger.warning("补抓异常 %s: %s", notice.url, e)
                continue
            if not (enriched.content_text or enriched.content_html):
                logger.warning("仍无正文: %s", notice.url)
                continue
            if enriched.is_bim_related is None:
                from src.core.bim_classifier import apply_bim_classification_to_notice

                enriched = apply_bim_classification_to_notice(enriched, rate_limit=True)
            if mongo_repo.upsert_notice(enriched):
                batch_updated += 1
                preview = (enriched.content_text or "")[:60].replace("\n", " ")
                logger.info("✓ %s | %s...", notice.title[:50], preview)
        total_updated += batch_updated
        logger.info(
            "站点 %s 第 %d 轮: 写入 %d/%d",
            site_id,
            rounds,
            batch_updated,
            len(docs),
        )
        if not until_empty:
            break
        more_docs, _ = collect_backfill_docs(mongo_repo, site_id, 1)
        if not more_docs:
            break
        if batch_updated == 0:
            logger.warning(
                "站点 %s 仍有待补详情但本轮零写入，避免死循环停止",
                site_id,
            )
            break

    status = "ok" if total_attempted or total_skipped_urls else "empty"
    return {
        "site_id": site_id,
        "status": status,
        "updated": total_updated,
        "attempted": total_attempted,
        "skipped_urls": total_skipped_urls,
        "rounds": rounds,
    }


def list_all_sites(sites_by_id: dict[str, dict[str, Any]]) -> tuple[list[str], list[dict[str, str]]]:
    supported: list[str] = []
    skipped: list[dict[str, str]] = []
    for site_id, site in sorted(sites_by_id.items()):
        if not site.get("enabled"):
            continue
        adapter_name = site.get("adapter", "generic")
        if adapter_supports_fetch_detail(adapter_name):
            supported.append(site_id)
        else:
            skipped.append(
                {
                    "site_id": site_id,
                    "adapter": adapter_name,
                    "reason": "适配器未实现 fetch_detail（如央企 SPA 等）",
                }
            )
    return supported, skipped


async def run_all_sites(
    mongo_repo: MongoRepository,
    sites_by_id: dict[str, dict[str, Any]],
    *,
    limit_per_site: int,
    until_empty: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    site_ids, skipped_meta = list_all_sites(sites_by_id)
    results: list[dict[str, Any]] = []
    pool = BrowserPool(headless=True)
    await pool.start()
    try:
        for site_id in site_ids:
            site = sites_by_id[site_id]
            logger.info("======== 开始补抓站点: %s ========", site_id)
            try:
                result = await backfill_site(
                    site_id,
                    site,
                    mongo_repo,
                    pool,
                    limit_per_batch=limit_per_site,
                    until_empty=until_empty,
                )
            except Exception as e:
                logger.exception("站点 %s 补抓失败（继续下一站）: %s", site_id, e)
                result = {
                    "site_id": site_id,
                    "status": "error",
                    "error": str(e),
                    "updated": 0,
                    "attempted": 0,
                }
            results.append(result)
            logger.info(
                "站点 %s 完成: updated=%s attempted=%s status=%s",
                site_id,
                result.get("updated"),
                result.get("attempted"),
                result.get("status"),
            )
    finally:
        await pool.stop()
    return results, skipped_meta


async def main() -> None:
    parser = argparse.ArgumentParser(description="补抓 MongoDB 公告正文")
    parser.add_argument("site_id", nargs="?", help="站点 ID，如 ccgp_national")
    parser.add_argument("--limit", type=int, default=5, help="单站最多补抓条数（非 --all-sites 模式）")
    parser.add_argument(
        "--all-sites",
        action="store_true",
        help="遍历所有 enabled 且适配器支持 fetch_detail 的站点",
    )
    parser.add_argument(
        "--limit-per-site",
        type=int,
        default=200,
        help="--all-sites 时每轮每站最多补抓条数",
    )
    parser.add_argument(
        "--until-empty",
        action="store_true",
        help="每站循环补抓直到无更多详情 URL 或本轮零写入",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=None,
        help="日志文件路径（默认 --all-sites 时写入 data/backfill_all_<ts>.log）",
    )
    args = parser.parse_args()

    if args.all_sites and args.site_id:
        parser.error("请指定 site_id 或使用 --all-sites，不可同时使用")
    if not args.all_sites and not args.site_id:
        parser.error("请提供 site_id 或使用 --all-sites")

    log_path = args.log
    if args.all_sites and log_path is None:
        stamp = datetime.now(TZ_CN).strftime("%Y%m%d_%H%M%S")
        log_path = DATA_DIR / f"backfill_all_{stamp}.log"
    setup_logging(log_path)

    settings = get_settings()
    mongo_repo = create_mongo_repository(
        uri=settings.mongodb_uri,
        db_name=settings.mongodb_db,
        collection_name=settings.mongodb_collection,
    )
    if not mongo_repo or not mongo_repo.available:
        logger.error("MongoDB 不可用")
        sys.exit(1)

    try:
        _, sites_by_id = load_sites_config()
        before = content_stats(mongo_repo)
        logger.info(
            "MongoDB 补抓前: total=%d with_content=%d without_content=%d",
            before["total"],
            before["with_content"],
            before["without_content"],
        )

        if args.all_sites:
            results, skipped_sites = await run_all_sites(
                mongo_repo,
                sites_by_id,
                limit_per_site=args.limit_per_site,
                until_empty=args.until_empty,
            )
            after = content_stats(mongo_repo)
            logger.info(
                "MongoDB 补抓后: total=%d with_content=%d without_content=%d",
                after["total"],
                after["with_content"],
                after["without_content"],
            )
            print("\n=== 各站补抓结果 ===")
            for r in results:
                print(
                    f"{r['site_id']}: updated={r.get('updated', 0)} "
                    f"attempted={r.get('attempted', 0)} status={r.get('status')}"
                )
            if skipped_sites:
                print("\n=== 跳过站点（不支持 fetch_detail）===")
                for s in skipped_sites:
                    print(f"{s['site_id']} ({s['adapter']}): {s['reason']}")
            print(
                f"\n汇总: 补抓前 without={before['without_content']} "
                f"→ 补抓后 without={after['without_content']} "
                f"(+{after['with_content'] - before['with_content']} with_content)"
            )
            if log_path:
                print(f"日志: {log_path}")
            return

        raw_site = sites_by_id.get(args.site_id)
        if not raw_site:
            logger.error("未找到站点: %s", args.site_id)
            sys.exit(1)

        pool = BrowserPool(headless=True)
        await pool.start()
        try:
            result = await backfill_site(
                args.site_id,
                raw_site,
                mongo_repo,
                pool,
                limit_per_batch=args.limit,
                until_empty=False,
            )
        finally:
            await pool.stop()

        if result.get("status") == "skipped":
            logger.error("%s", result.get("reason"))
            sys.exit(1)
        if result.get("status") == "empty":
            print(
                f"站点 {args.site_id} 无待补抓详情页记录"
                f"（已跳过 {result.get('skipped_urls', 0)} 条列表/导航 URL）"
            )
            return
        print(
            f"\n补抓完成: {result['updated']}/{result['attempted']} 条已写入 MongoDB"
        )
    finally:
        mongo_repo.close()


if __name__ == "__main__":
    asyncio.run(main())
