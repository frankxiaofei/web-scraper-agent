#!/usr/bin/env python3
"""为已有正文的公告补提取关键项目信息。"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.config import get_settings
from src.core.key_info_extractor import apply_key_info_to_notice
from src.core.models import BidNotice, NoticeCategory
from src.db.mongo_repository import create_mongo_repository

TZ_CN = timezone(timedelta(hours=8))
logger = logging.getLogger("backfill_key_info")

KEY_FIELD_NAMES = (
    "contract_amount",
    "budget_amount",
    "tender_party",
    "project_period",
    "key_summary",
    "bid_deadline",
    "open_time",
    "contact",
)


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
        contract_amount=doc.get("contract_amount"),
        budget_amount=doc.get("budget_amount"),
        tender_party=doc.get("tender_party"),
        project_period=doc.get("project_period"),
        key_summary=doc.get("key_summary"),
        bid_deadline=doc.get("bid_deadline"),
        open_time=doc.get("open_time"),
        contact=doc.get("contact"),
    )


def _needs_key_info(doc: dict[str, Any]) -> bool:
    text = (doc.get("content_text") or "").strip()
    html = (doc.get("content_html") or "").strip()
    if not text and not html:
        return False
    for field in KEY_FIELD_NAMES:
        if doc.get(field):
            return False
    if doc.get("purchaser") or doc.get("budget"):
        return False
    return True


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
            {"content_text": {"$exists": True, "$nin": [None, ""]}},
            {"content_html": {"$exists": True, "$nin": [None, ""]}},
        ]
    }
    if source:
        query = {
            "$and": [
                query,
                {"$or": [{"source_site_id": source}, {"source": source}, {"site_id": source}]},
            ]
        }

    cursor = col.find(query).sort("crawled_at", -1).limit(limit * 3 if not force else limit)
    docs: list[dict[str, Any]] = []
    for doc in cursor:
        if force or _needs_key_info(doc):
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
        before = notice_key_info_snapshot(notice)
        enriched = apply_key_info_to_notice(notice)
        after = notice_key_info_snapshot(enriched)
        if before == after and not force:
            continue
        if mongo_repo.upsert_notice(enriched):
            updated += 1
            logger.info(
                "✓ %s | 预算=%s 招标方=%s",
                (enriched.title or "")[:40],
                enriched.budget_amount or enriched.budget or "—",
                enriched.tender_party or enriched.purchaser or "—",
            )

    return {"attempted": len(docs), "updated": updated}


def notice_key_info_snapshot(notice: BidNotice) -> tuple:
    return tuple(getattr(notice, f) or "" for f in KEY_FIELD_NAMES)


def main() -> int:
    parser = argparse.ArgumentParser(description="为已有正文补提取关键项目信息")
    parser.add_argument("--source", help="限定站点 ID（sites.yaml 中的 id）")
    parser.add_argument("--limit", type=int, default=100, help="最多处理条数")
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重新提取（即使已有部分关键字段）",
    )
    parser.add_argument(
        "--log",
        type=Path,
        help="日志文件路径（默认 data/backfill_key_info_<timestamp>.log）",
    )
    args = parser.parse_args()

    ts = datetime.now(TZ_CN).strftime("%Y%m%d_%H%M%S")
    log_path = args.log or (ROOT / "data" / f"backfill_key_info_{ts}.log")
    setup_logging(log_path)

    logger.info("开始补提取关键信息 source=%s limit=%d force=%s", args.source, args.limit, args.force)
    stats = run_backfill(source=args.source, limit=args.limit, force=args.force)
    logger.info("完成: 处理 %d 条，更新 %d 条", stats["attempted"], stats["updated"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
