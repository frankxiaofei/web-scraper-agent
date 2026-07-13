#!/usr/bin/env python3
"""为已有公告补抽取结构化字段（合同金额、招标方、项目周期、招标关键内容）。"""

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
from src.core.notice_field_utils import extract_notice_fields_from_doc
from src.db.mongo_repository import create_mongo_repository
from src.web.notice_extract_skills import extract_notice_fields

TZ_CN = timezone(timedelta(hours=8))
logger = logging.getLogger("backfill_notice_fields")

TARGET_FIELDS = ("contract_amount", "tender_party", "project_period", "key_summary")


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


def _needs_extraction(doc: dict[str, Any]) -> bool:
    text = (doc.get("content_text") or "").strip()
    summary = (doc.get("key_summary") or "").strip()
    if not text and not summary:
        return False
    for field in TARGET_FIELDS:
        if not doc.get(field):
            return True
    if summary and ("和范围" in summary or "交货期/工期" in summary):
        return True
    return False


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
            {"key_summary": {"$exists": True, "$nin": [None, ""]}},
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
        if force or _needs_extraction(doc):
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
        before = {f: doc.get(f) for f in TARGET_FIELDS}
        fields = extract_notice_fields_from_doc(doc, use_llm=False)
        result = extract_notice_fields(doc=doc, persist=True, use_llm=False)
        if not result.get("persisted") and not force:
            continue
        after = {
            "contract_amount": fields.get("contract_amount"),
            "tender_party": fields.get("tender_party"),
            "project_period": fields.get("project_period"),
            "key_summary": fields.get("key_content"),
        }
        if before != after or force:
            updated += 1
            logger.info(
                "✓ %s | 金额=%s 招标方=%s 周期=%s",
                (doc.get("title") or "")[:40],
                after.get("contract_amount") or "—",
                after.get("tender_party") or "—",
                after.get("project_period") or "—",
            )

    return {"attempted": len(docs), "updated": updated}


def main() -> int:
    parser = argparse.ArgumentParser(description="补抽取公告结构化字段")
    parser.add_argument("--source", help="限定站点 ID（sites.yaml 中的 id）")
    parser.add_argument("--limit", type=int, default=100, help="最多处理条数")
    parser.add_argument("--force", action="store_true", help="强制重新抽取并写回")
    parser.add_argument(
        "--log",
        type=Path,
        help="日志文件路径（默认 data/backfill_notice_fields_<timestamp>.log）",
    )
    args = parser.parse_args()

    ts = datetime.now(TZ_CN).strftime("%Y%m%d_%H%M%S")
    log_path = args.log or (ROOT / "data" / f"backfill_notice_fields_{ts}.log")
    setup_logging(log_path)

    logger.info("开始补抽取结构化字段 source=%s limit=%d force=%s", args.source, args.limit, args.force)
    stats = run_backfill(source=args.source, limit=args.limit, force=args.force)
    logger.info("完成: 处理 %d 条，更新 %d 条", stats["attempted"], stats["updated"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
