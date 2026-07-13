#!/usr/bin/env python3
"""为 is_bim_related=True 的 BIM 公告补抓/重抓详情页正文。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backfill_content import (  # noqa: E402
    adapter_supports_fetch_detail,
    load_sites_config,
)
from src.core.browser import BrowserPool  # noqa: E402
from src.core.config import get_settings  # noqa: E402
from src.core.key_info_extractor import apply_key_info_to_notice  # noqa: E402
from src.core.models import BidNotice, NoticeCategory  # noqa: E402
from src.core.scheduler import get_adapter_class  # noqa: E402
from src.core.crawl_rules import load_crawl_rule  # noqa: E402
from src.db.mongo_repository import MongoRepository, create_mongo_repository  # noqa: E402
from src.core.site_sync import get_crawl_scope_label, load_enabled_sites  # noqa: E402
from src.web.data_service import NoticeDataService  # noqa: E402

TZ_CN = timezone(timedelta(hours=8))
logger = logging.getLogger("backfill_bim_content")
DEFAULT_BATCH_SIZE = 50


def site_supports_fetch_detail(site: dict[str, Any]) -> bool:
    """判断站点是否可补抓详情（含 generic + crawl_rules.detail.fetch_detail）。"""
    adapter_name = site.get("adapter", "generic")
    if adapter_name != "generic":
        return adapter_supports_fetch_detail(adapter_name)
    rule = load_crawl_rule(site)
    return bool(rule and rule.detail and rule.detail.fetch_detail)

# 正文过短阈值（字符数，strip 后）
MIN_CONTENT_TEXT_LEN = 400
MIN_CONTENT_HTML_LEN = 500
# 公共资源平台类站点：无 table 且正文偏短时视为不全
GGZY_NO_TABLE_MAX_TEXT = 1200
GGZY_SITE_PREFIX = "ggzy_"


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


def _scope_site_ids(source: str | None) -> list[str]:
    enabled = [s["id"] for s in load_enabled_sites() if s.get("id")]
    if source:
        return [source] if source in enabled else []
    return enabled


def _mvp_source_filter(site_ids: list[str]) -> dict[str, Any]:
    return NoticeDataService._mvp_source_filter_mongo(site_ids)


def _doc_source_id(doc: dict[str, Any]) -> str:
    return doc.get("source_site_id") or doc.get("source") or doc.get("site_id") or ""


def is_incomplete_content(doc: dict[str, Any]) -> tuple[bool, str]:
    """判断公告正文是否不全，返回 (是否不全, 原因码)。"""
    text = (doc.get("content_text") or "").strip()
    html = (doc.get("content_html") or "").strip()

    if not text and not html:
        return True, "empty"

    if len(text) >= MIN_CONTENT_TEXT_LEN or len(html) >= MIN_CONTENT_HTML_LEN:
        site_id = _doc_source_id(doc)
        if site_id.startswith(GGZY_SITE_PREFIX) and html and "<table" not in html.lower():
            if len(text) < GGZY_NO_TABLE_MAX_TEXT:
                return True, "ggzy_no_table"
        return False, ""

    return True, "short_text"


def build_bim_content_query(
    site_ids: list[str],
    *,
    source: str | None,
) -> dict[str, Any]:
    if not site_ids:
        return {"_id": {"$exists": False}}

    clauses: list[dict[str, Any]] = [
        _mvp_source_filter(site_ids),
        {"is_bim_related": True},
    ]
    if source:
        clauses.append(
            {"$or": [{"source_site_id": source}, {"source": source}, {"site_id": source}]}
        )
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def _doc_to_notice(doc: dict[str, Any], site: dict[str, Any] | None = None) -> BidNotice:
    site_id = _doc_source_id(doc)
    cat_raw = doc.get("category") or NoticeCategory.TENDER.value
    try:
        category = NoticeCategory(cat_raw)
    except ValueError:
        category = NoticeCategory.TENDER
    return BidNotice(
        title=doc.get("title") or "",
        url=doc.get("url") or doc.get("detail_url") or "",
        source_site_id=site_id,
        source_site_name=doc.get("source_site_name") or (site or {}).get("name", site_id),
        source_url=doc.get("source_url") or (site or {}).get("url", ""),
        content_hash=doc.get("content_hash"),
        category=category,
        purchaser=doc.get("purchaser"),
        agency=doc.get("agency"),
        budget=doc.get("budget"),
        content_text=doc.get("content_text"),
        content_html=doc.get("content_html"),
        key_summary=doc.get("key_summary"),
        is_bim_related=doc.get("is_bim_related"),
        bim_confidence=doc.get("bim_confidence"),
        bim_reason=doc.get("bim_reason"),
        bim_tags=doc.get("bim_tags"),
        bim_classified_at=doc.get("bim_classified_at"),
    )


def get_content_stats(
    mongo_repo: MongoRepository,
    *,
    source: str | None = None,
) -> dict[str, Any]:
    site_ids = _scope_site_ids(source)
    if not mongo_repo.available or mongo_repo._notices is None or not site_ids:
        return {
            "scope_label": get_crawl_scope_label(),
            "site_count": len(site_ids),
            "bim_total": 0,
            "incomplete": 0,
            "complete": 0,
            "by_reason": {},
            "adapter_unsupported": 0,
        }

    col = mongo_repo._notices
    query = build_bim_content_query(site_ids, source=source)
    _, sites_by_id = load_sites_config()

    bim_total = 0
    incomplete = 0
    complete = 0
    by_reason: dict[str, int] = {}
    adapter_unsupported = 0

    cursor = col.find(query).sort([("crawled_at", -1), ("scraped_at", -1), ("_id", -1)])
    for doc in cursor:
        bim_total += 1
        bad, reason = is_incomplete_content(doc)
        if bad:
            incomplete += 1
            by_reason[reason] = by_reason.get(reason, 0) + 1
        else:
            complete += 1
        site_id = _doc_source_id(doc)
        site = sites_by_id.get(site_id) or {}
        if not site_supports_fetch_detail(site):
            adapter_unsupported += 1

    return {
        "scope_label": get_crawl_scope_label(),
        "site_count": len(site_ids),
        "bim_total": bim_total,
        "incomplete": incomplete,
        "complete": complete,
        "by_reason": by_reason,
        "adapter_unsupported": adapter_unsupported,
    }


def iter_bim_content_docs(
    col,
    query: dict[str, Any],
    *,
    limit: int,
    force: bool,
    batch_size: int,
) -> Iterator[dict[str, Any]]:
    cursor = (
        col.find(query)
        .sort([("crawled_at", -1), ("scraped_at", -1), ("_id", -1)])
        .batch_size(batch_size)
    )
    yielded = 0
    for doc in cursor:
        if not force:
            bad, _ = is_incomplete_content(doc)
            if not bad:
                continue
        url = doc.get("url") or doc.get("detail_url") or ""
        reason = MongoRepository.missing_content_skip_reason(doc)
        if reason:
            logger.info("跳过补抓 (%s): %s", reason, url)
            continue
        yield doc
        yielded += 1
        if limit > 0 and yielded >= limit:
            break


async def _fetch_and_upsert(
    doc: dict[str, Any],
    site: dict[str, Any],
    mongo_repo: MongoRepository,
    pool: BrowserPool,
) -> tuple[bool, str]:
    site_id = _doc_source_id(doc)
    adapter_name = site.get("adapter", "generic")
    if not site_supports_fetch_detail(site):
        return False, "unsupported_adapter"

    adapter_cls = get_adapter_class(adapter_name)
    adapter = adapter_cls(site, pool)
    notice = _doc_to_notice(doc, site)
    if not notice.url:
        return False, "empty_url"

    try:
        enriched = await adapter.fetch_detail(notice)
        enriched = apply_key_info_to_notice(enriched)
    except Exception as exc:
        logger.warning("补抓异常 %s: %s", notice.url, exc)
        return False, "fetch_error"

    if not (enriched.content_text or enriched.content_html):
        logger.warning("仍无正文: %s", notice.url)
        return False, "still_empty"

    # 保留已有 BIM 标注，避免 upsert 清空字段
    enriched.is_bim_related = doc.get("is_bim_related")
    enriched.bim_confidence = doc.get("bim_confidence")
    enriched.bim_reason = doc.get("bim_reason")
    enriched.bim_tags = doc.get("bim_tags")
    enriched.bim_classified_at = doc.get("bim_classified_at")

    if mongo_repo.upsert_notice(enriched):
        preview = (enriched.content_text or "")[:60].replace("\n", " ")
        logger.info("✓ %s | %s...", (notice.title or "")[:50], preview)
        return True, "updated"
    return False, "upsert_failed"


async def run_backfill_async(
    *,
    source: str | None = None,
    limit: int = 100,
    force: bool = False,
    dry_run: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
    from src.core.powerchina_notice import POWERCHINA_SITE_ID

    if source == POWERCHINA_SITE_ID:
        from scripts.backfill_powerchina_bim_content import run_backfill_async as run_powerchina

        logger.info(
            "电建站走专用补抓脚本（修正 search 占位 URL / ID 映射）: backfill_powerchina_bim_content.py"
        )
        return await run_powerchina(
            limit=limit,
            force=force,
            dry_run=dry_run,
            batch_size=batch_size,
        )

    site_ids = _scope_site_ids(source)
    if source and not site_ids:
        logger.error("站点 %s 不在当前 crawl_scope 或未 enabled", source)
        return {"attempted": 0, "updated": 0, "skipped": 0, "dry_run": dry_run}

    settings = get_settings()
    mongo_repo = create_mongo_repository(
        uri=settings.mongodb_uri,
        db_name=settings.mongodb_db,
        collection_name=settings.mongodb_collection,
    )
    if not mongo_repo or not mongo_repo.available:
        logger.error("MongoDB 不可用，请配置 MONGODB_URI")
        return {"attempted": 0, "updated": 0, "skipped": 0, "dry_run": dry_run}

    stats_before = get_content_stats(mongo_repo, source=source)
    logger.info(
        "BIM 正文范围: %s (%d 站) | BIM 总计 %d | 正文不全 %d | 正文完整 %d",
        stats_before["scope_label"],
        stats_before["site_count"],
        stats_before["bim_total"],
        stats_before["incomplete"],
        stats_before["complete"],
    )
    if stats_before.get("by_reason"):
        logger.info("不全原因分布: %s", stats_before["by_reason"])

    query = build_bim_content_query(site_ids, source=source)
    col = mongo_repo._notices

    pending = 0
    if col is not None:
        if force:
            for doc in col.find(query):
                url = doc.get("url") or doc.get("detail_url") or ""
                if MongoRepository.missing_content_skip_reason(doc) is None:
                    pending += 1
        else:
            for doc in col.find(query):
                bad, _ = is_incomplete_content(doc)
                if not bad:
                    continue
                if MongoRepository.missing_content_skip_reason(doc) is None:
                    pending += 1

    would_process = min(limit, pending) if limit > 0 else pending

    if dry_run:
        mongo_repo.close()
        return {
            **stats_before,
            "pending": pending,
            "would_process": would_process,
            "attempted": 0,
            "updated": 0,
            "skipped": 0,
            "dry_run": True,
            "force": force,
        }

    _, sites_by_id = load_sites_config()
    attempted = updated = skipped = 0
    pool = BrowserPool(headless=True)
    await pool.start()
    try:
        for doc in iter_bim_content_docs(
            col,
            query,
            limit=limit,
            force=force,
            batch_size=batch_size,
        ):
            attempted += 1
            site_id = _doc_source_id(doc)
            site = sites_by_id.get(site_id)
            if not site:
                logger.warning("未找到站点配置: %s", site_id)
                skipped += 1
                continue
            ok, reason = await _fetch_and_upsert(doc, site, mongo_repo, pool)
            if ok:
                updated += 1
            else:
                skipped += 1
                if reason == "unsupported_adapter":
                    logger.info(
                        "跳过（适配器不支持 fetch_detail）: %s",
                        doc.get("url") or doc.get("detail_url"),
                    )

            if attempted % batch_size == 0:
                logger.info(
                    "进度: 已处理 %d 条，更新 %d 条，跳过 %d 条",
                    attempted,
                    updated,
                    skipped,
                )
    finally:
        await pool.stop()

    stats_after = get_content_stats(mongo_repo, source=source)
    mongo_repo.close()
    return {
        "attempted": attempted,
        "updated": updated,
        "skipped": skipped,
        "dry_run": False,
        "force": force,
        "before": stats_before,
        "after": stats_after,
    }


def run_backfill(
    *,
    source: str | None = None,
    limit: int = 100,
    force: bool = False,
    dry_run: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
    return asyncio.run(
        run_backfill_async(
            source=source,
            limit=limit,
            force=force,
            dry_run=dry_run,
            batch_size=batch_size,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="为 BIM 相关公告补抓/重抓详情页正文")
    parser.add_argument("--source", help="限定站点 ID（sites.yaml 中的 id）")
    parser.add_argument("--limit", type=int, default=100, help="最多处理条数（0=不限）")
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重抓所有 BIM 公告（即使正文看似完整）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅统计 BIM 正文不全数量，不发起补抓",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="MongoDB 游标批量大小与进度日志间隔",
    )
    parser.add_argument(
        "--log",
        type=Path,
        help="日志文件路径（默认 data/backfill_bim_content_<timestamp>.log）",
    )
    args = parser.parse_args()

    ts = datetime.now(TZ_CN).strftime("%Y%m%d_%H%M%S")
    log_path = None if args.dry_run else (args.log or (ROOT / "data" / f"backfill_bim_content_{ts}.log"))
    setup_logging(log_path)

    logger.info(
        "开始 BIM 正文补抓 source=%s limit=%d force=%s dry_run=%s",
        args.source,
        args.limit,
        args.force,
        args.dry_run,
    )
    stats = run_backfill(
        source=args.source,
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
        batch_size=max(1, args.batch_size),
    )

    if args.dry_run:
        logger.info(
            "dry-run 完成: BIM 总计 %d | 正文不全 %d | 正文完整 %d | 待补抓 %d | 本次将处理 %d",
            stats.get("bim_total", 0),
            stats.get("incomplete", 0),
            stats.get("complete", 0),
            stats.get("pending", 0),
            stats.get("would_process", 0),
        )
        if stats.get("by_reason"):
            logger.info("不全原因: %s", stats["by_reason"])
    else:
        after = stats.get("after") or {}
        logger.info(
            "完成: 处理 %d 条，更新 %d 条，跳过 %d 条 | 正文不全剩余 %d",
            stats.get("attempted", 0),
            stats.get("updated", 0),
            stats.get("skipped", 0),
            after.get("incomplete", "—"),
        )
        if log_path:
            logger.info("日志: %s", log_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
