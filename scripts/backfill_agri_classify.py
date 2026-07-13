#!/usr/bin/env python3
"""为已有 MongoDB 公告补做智慧农业相关性分类。"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.agri_classifier import (
    apply_agri_classification_to_notice,
    is_agri_classify_available,
    is_agri_classify_enabled,
)
from src.core.config import get_settings
from src.core.models import BidNotice, NoticeCategory
from src.db.mongo_repository import create_mongo_repository

TZ_CN = timezone(timedelta(hours=8))
logger = logging.getLogger("backfill_agri_classify")


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
        is_agri_related=doc.get("is_agri_related"),
        agri_confidence=doc.get("agri_confidence"),
        agri_reason=doc.get("agri_reason"),
        agri_tags=doc.get("agri_tags"),
    )


def _needs_classify(doc: dict[str, Any]) -> bool:
    if doc.get("is_agri_related") is not None:
        return False
    text = (doc.get("content_text") or "").strip()
    title = (doc.get("title") or "").strip()
    return bool(text or title)


def find_docs(
    mongo_repo,
    *,
    source: str | None,
    limit: int,
    force: bool,
) -> list[dict[str, Any]]:
    if not mongo_repo.available or mongo_repo._notices is None:
        return []
    col = mongo_repo._notices
    query: dict[str, Any] = {
        "$or": [
            {"title": {"$exists": True, "$nin": [None, ""]}},
            {"content_text": {"$exists": True, "$nin": [None, ""]}},
        ]
    }
    if source:
        query = {
            "$and": [
                query,
                {"$or": [{"source_site_id": source}, {"source": source}, {"site_id": source}]},
            ]
        }
    if not force:
        query = {
            "$and": [
                query,
                {"$or": [{"is_agri_related": {"$exists": False}}, {"is_agri_related": None}]},
            ]
        }

    cursor = col.find(query).sort("crawled_at", -1).limit(limit * 3 if not force else limit)
    docs: list[dict[str, Any]] = []
    for doc in cursor:
        if force or _needs_classify(doc):
            docs.append(doc)
        if len(docs) >= limit:
            break
    return docs


def run_backfill(
    *,
    source: str | None,
    limit: int,
    force: bool,
) -> dict[str, int]:
    if not is_agri_classify_available():
        logger.error("农业分类不可用")
        return {"attempted": 0, "updated": 0}
    if not is_agri_classify_enabled():
        logger.warning("LLM 未启用，将使用关键词 fallback 分类")

    settings = get_settings()
    mongo_repo = create_mongo_repository(
        uri=settings.mongodb_uri,
        db_name=settings.mongodb_db,
        collection_name=settings.mongodb_collection,
    )
    if not mongo_repo or not mongo_repo.available:
        logger.error("MongoDB 不可用，请配置 MONGODB_URI")
        return {"attempted": 0, "updated": 0}

    docs = find_docs(mongo_repo, source=source, limit=limit, force=force)
    updated = 0
    for doc in docs:
        notice = _doc_to_notice(doc)
        before = notice.is_agri_related
        enriched = apply_agri_classification_to_notice(notice, rate_limit=True, force=force)
        if enriched.is_agri_related == before and not force:
            continue
        if mongo_repo.upsert_notice(enriched):
            updated += 1
            label = (
                "农业"
                if enriched.is_agri_related
                else ("非农业" if enriched.is_agri_related is False else "未分类")
            )
            logger.info(
                "✓ %s | %s | conf=%s",
                (enriched.title or "")[:40],
                label,
                enriched.agri_confidence if enriched.agri_confidence is not None else "—",
            )

    mongo_repo.close()
    return {"attempted": len(docs), "updated": updated}


def main() -> int:
    parser = argparse.ArgumentParser(description="为已有公告补做智慧农业相关性分类")
    parser.add_argument("--source", help="限定站点 ID（sites.yaml 中的 id）")
    parser.add_argument("--limit", type=int, default=100, help="最多处理条数")
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重新分类（即使已有 is_agri_related）",
    )
    parser.add_argument(
        "--log",
        type=Path,
        help="日志文件路径（默认 data/backfill_agri_classify_<timestamp>.log）",
    )
    args = parser.parse_args()

    ts = datetime.now(TZ_CN).strftime("%Y%m%d_%H%M%S")
    log_path = args.log or (ROOT / "data" / f"backfill_agri_classify_{ts}.log")
    setup_logging(log_path)

    logger.info("开始农业补分类 source=%s limit=%d force=%s", args.source, args.limit, args.force)
    stats = run_backfill(source=args.source, limit=args.limit, force=args.force)
    logger.info("完成: 处理 %d 条，更新 %d 条", stats["attempted"], stats["updated"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
