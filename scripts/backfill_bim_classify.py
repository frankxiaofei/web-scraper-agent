#!/usr/bin/env python3
"""为已有 MongoDB 公告补做 BIM 相关性分类（crawl_scope 范围内）。"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.bim_classifier import (
    apply_bim_classification_to_notice,
    is_bim_classify_available,
    is_bim_classify_enabled,
)
from src.core.config import get_settings
from src.core.models import BidNotice, NoticeCategory
from src.core.site_sync import get_crawl_scope_label, load_enabled_sites
from src.db.mongo_repository import create_mongo_repository
from src.web.data_service import NoticeDataService

TZ_CN = timezone(timedelta(hours=8))
logger = logging.getLogger("backfill_bim_classify")
DEFAULT_BATCH_SIZE = 50


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


def _doc_to_notice(doc: dict[str, Any]) -> BidNotice:
    cat_raw = doc.get("category") or NoticeCategory.TENDER.value
    try:
        category = NoticeCategory(cat_raw)
    except ValueError:
        category = NoticeCategory.TENDER
    return BidNotice(
        title=doc.get("title") or "",
        url=doc.get("url") or doc.get("detail_url") or "",
        source_site_id=doc.get("source_site_id") or doc.get("source") or doc.get("site_id") or "",
        source_site_name=doc.get("source_site_name") or "",
        source_url=doc.get("source_url") or "",
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


def _needs_classify(doc: dict[str, Any]) -> bool:
    if doc.get("is_bim_related") is not None:
        return False
    text = (doc.get("content_text") or "").strip()
    title = (doc.get("title") or "").strip()
    return bool(text or title)


def build_backfill_query(
    site_ids: list[str],
    *,
    source: str | None,
    force: bool,
) -> dict[str, Any]:
    if not site_ids:
        return {"_id": {"$exists": False}}

    clauses: list[dict[str, Any]] = [
        _mvp_source_filter(site_ids),
        {
            "$or": [
                {"title": {"$exists": True, "$nin": [None, ""]}},
                {"content_text": {"$exists": True, "$nin": [None, ""]}},
            ]
        },
    ]
    if source:
        clauses.append(
            {"$or": [{"source_site_id": source}, {"source": source}, {"site_id": source}]}
        )
    if not force:
        clauses.append(
            {
                "$or": [
                    {"is_bim_related": {"$exists": False}},
                    {"is_bim_related": None},
                ]
            }
        )
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def get_label_stats(mongo_repo, *, source: str | None = None) -> dict[str, Any]:
    """统计 crawl_scope 范围内 BIM 打标情况。"""
    site_ids = _scope_site_ids(source)
    if not mongo_repo.available or mongo_repo._notices is None or not site_ids:
        return {
            "scope_label": get_crawl_scope_label(),
            "site_count": len(site_ids),
            "total": 0,
            "bim_true": 0,
            "bim_false": 0,
            "bim_unclassified": 0,
            "bim_classified": 0,
        }

    col = mongo_repo._notices
    base = _mvp_source_filter(site_ids)
    bim_true = col.count_documents({**base, "is_bim_related": True})
    bim_false = col.count_documents({**base, "is_bim_related": False})
    bim_unclassified = col.count_documents(
        {
            "$and": [
                base,
                {
                    "$or": [
                        {"is_bim_related": {"$exists": False}},
                        {"is_bim_related": None},
                    ]
                },
            ]
        }
    )
    total = col.count_documents(base)
    return {
        "scope_label": get_crawl_scope_label(),
        "site_count": len(site_ids),
        "total": total,
        "bim_true": bim_true,
        "bim_false": bim_false,
        "bim_unclassified": bim_unclassified,
        "bim_classified": bim_true + bim_false,
    }


def iter_backfill_docs(
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
        if not force and not _needs_classify(doc):
            continue
        yield doc
        yielded += 1
        if limit > 0 and yielded >= limit:
            break


def run_backfill(
    *,
    source: str | None = None,
    limit: int = 100,
    force: bool = False,
    dry_run: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
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

    stats_before = get_label_stats(mongo_repo, source=source)
    logger.info(
        "数据范围: %s (%d 站) | 总计 %d | 已分析 %d (BIM %d / 非BIM %d) | 待打标 %d",
        stats_before["scope_label"],
        stats_before["site_count"],
        stats_before["total"],
        stats_before["bim_classified"],
        stats_before["bim_true"],
        stats_before["bim_false"],
        stats_before["bim_unclassified"],
    )

    query = build_backfill_query(site_ids, source=source, force=force)
    col = mongo_repo._notices
    pending = col.count_documents(query) if col is not None else 0
    would_process = min(limit, pending) if limit > 0 else pending

    if dry_run:
        mongo_repo.close()
        result = {
            **stats_before,
            "pending": pending,
            "would_process": would_process,
            "attempted": 0,
            "updated": 0,
            "skipped": 0,
            "dry_run": True,
            "force": force,
        }
        logger.info(
            "dry-run: 待处理 %d 条，本次 limit=%d 将处理 %d 条 (force=%s)",
            pending,
            limit,
            would_process,
            force,
        )
        return result

    if not is_bim_classify_available():
        logger.error("BIM 分类不可用（需 BIM_CLASSIFY_LLM+OPENAI_API_KEY 或关键词 fallback）")
        mongo_repo.close()
        return {"attempted": 0, "updated": 0, "skipped": 0, "dry_run": False}
    if not is_bim_classify_enabled():
        logger.warning("LLM 未启用，将使用关键词 fallback 分类")

    attempted = updated = skipped = 0
    for doc in iter_backfill_docs(
        col,
        query,
        limit=limit,
        force=force,
        batch_size=batch_size,
    ):
        attempted += 1
        notice = _doc_to_notice(doc)
        before_related = notice.is_bim_related
        enriched = apply_bim_classification_to_notice(notice, rate_limit=True, force=force)
        if enriched.is_bim_related is None:
            skipped += 1
            continue
        if enriched.is_bim_related == before_related and not force:
            skipped += 1
            continue
        if mongo_repo.upsert_notice(enriched):
            updated += 1
            label = (
                "BIM"
                if enriched.is_bim_related
                else ("非BIM" if enriched.is_bim_related is False else "未分类")
            )
            logger.info(
                "✓ [%d] %s | %s | conf=%s",
                attempted,
                (enriched.title or "")[:40],
                label,
                enriched.bim_confidence if enriched.bim_confidence is not None else "—",
            )
        else:
            skipped += 1

        if attempted % batch_size == 0:
            logger.info(
                "进度: 已处理 %d 条，更新 %d 条，跳过 %d 条",
                attempted,
                updated,
                skipped,
            )

    stats_after = get_label_stats(mongo_repo, source=source)
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


def main() -> int:
    parser = argparse.ArgumentParser(description="为已有公告补做 BIM 相关性分类")
    parser.add_argument("--source", help="限定站点 ID（sites.yaml 中的 id）")
    parser.add_argument("--limit", type=int, default=100, help="最多处理条数（0=不限）")
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重新分类（即使已有 is_bim_related）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅统计待打标数量，不写入 MongoDB",
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
        help="日志文件路径（默认 data/backfill_bim_classify_<timestamp>.log）",
    )
    args = parser.parse_args()

    ts = datetime.now(TZ_CN).strftime("%Y%m%d_%H%M%S")
    log_path = None if args.dry_run else (args.log or (ROOT / "data" / f"backfill_bim_classify_{ts}.log"))
    setup_logging(log_path)

    logger.info(
        "开始 BIM 补分类 source=%s limit=%d force=%s dry_run=%s",
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
            "dry-run 完成: 总计 %d | 已分析 %d | BIM %d | 待打标 %d | 本次将处理 %d",
            stats.get("total", 0),
            stats.get("bim_classified", 0),
            stats.get("bim_true", 0),
            stats.get("bim_unclassified", 0),
            stats.get("would_process", 0),
        )
    else:
        after = stats.get("after") or {}
        logger.info(
            "完成: 处理 %d 条，更新 %d 条，跳过 %d 条 | 待打标剩余 %d",
            stats.get("attempted", 0),
            stats.get("updated", 0),
            stats.get("skipped", 0),
            after.get("bim_unclassified", "—"),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
