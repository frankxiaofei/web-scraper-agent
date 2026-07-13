#!/usr/bin/env python3
"""回填 Mongo bid_notices 的 publish_date / published_at。

优先级：
1. 电建 allList publishTime 索引（notice/detail?id=）
2. 电建 getInfo publishTime
3. 正文「公告发布时间」正则
4. 已有 published_at 字段

用法:
    python scripts/backfill_publish_date.py --dry-run --limit 20 --bim-only
    python scripts/backfill_publish_date.py --bim-only --fix-suspicious
    python scripts/backfill_publish_date.py --source 中国电力建设集团有限公司_公共资源交易服务平台

存量误用截止时间为 publish_date 的记录，可用 --fix-suspicious 重跑回填。
"""

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

from scripts.backfill_bim_content import build_bim_content_query, setup_logging  # noqa: E402
from src.core.config import get_settings  # noqa: E402
from src.core.powerchina_notice import (  # noqa: E402
    POWERCHINA_SITE_ID,
    build_id_publish_index,
    extract_notice_id,
    extract_publish_time_from_get_info,
    fetch_get_info,
)
from src.core.publish_date_utils import (  # noqa: E402
    doc_publish_day,
    extract_publish_date_from_content,
    parse_publish_datetime,
    publish_date_day,
    publish_date_suspicious,
    serialize_publish_date,
)
from src.core.site_sync import load_enabled_sites  # noqa: E402
from src.db.mongo_repository import create_mongo_repository  # noqa: E402
from src.web.data_service import NoticeDataService  # noqa: E402

TZ_CN = timezone(timedelta(hours=8))
logger = logging.getLogger("backfill_publish_date")


def _doc_url(doc: dict[str, Any]) -> str:
    return doc.get("url") or doc.get("detail_url") or ""


def build_query(
    *,
    bim_only: bool,
    source: str | None,
    only_missing: bool,
    force: bool,
) -> dict[str, Any]:
    site_ids = [s["id"] for s in load_enabled_sites() if s.get("id")]
    if not site_ids:
        return {"_id": {"$exists": False}}

    if bim_only:
        query = build_bim_content_query(site_ids, source=source)
    else:
        query = NoticeDataService._mvp_source_filter_mongo(site_ids)
        if source:
            query = {
                "$and": [
                    query,
                    {
                        "$or": [
                            {"source_site_id": source},
                            {"source": source},
                            {"site_id": source},
                        ]
                    },
                ]
            }

    if force or not only_missing:
        return query

    missing_clause = {
        "$or": [
            {"publish_date": {"$exists": False}},
            {"publish_date": None},
            {"publish_date": ""},
        ]
    }
    if isinstance(query, dict) and "$and" in query:
        return {"$and": [query, missing_clause]}
    return {"$and": [query, missing_clause]}


def should_process_doc(
    doc: dict[str, Any],
    *,
    only_missing: bool,
    fix_suspicious: bool,
    force: bool,
) -> bool:
    if force:
        return True
    missing = not doc_publish_day(doc)
    if only_missing:
        return missing
    if fix_suspicious:
        content_day = publish_date_day(extract_publish_date_from_content(doc.get("content_text")))
        current_day = doc_publish_day(doc)
        if content_day and current_day and content_day != current_day:
            return True
        return missing or publish_date_suspicious(doc)
    return missing


async def resolve_publish_date(
    doc: dict[str, Any],
    *,
    id_publish_index: dict[str, datetime],
    fetch_get_info_api: bool,
    get_info_cache: dict[str, datetime | None],
) -> tuple[datetime | None, str]:
    url = _doc_url(doc)
    notice_id = extract_notice_id(url) if "powerchina.cn" in url else None
    if notice_id:
        if notice_id in id_publish_index:
            return id_publish_index[notice_id], "allList_index"
        if fetch_get_info_api:
            if notice_id not in get_info_cache:
                data = await fetch_get_info(notice_id)
                get_info_cache[notice_id] = (
                    extract_publish_time_from_get_info(data) if data else None
                )
            cached = get_info_cache.get(notice_id)
            if cached:
                return cached, "getInfo"

    content_date = extract_publish_date_from_content(doc.get("content_text"))
    if content_date:
        return content_date, "content_text"

    for key in ("published_at",):
        parsed = parse_publish_datetime(doc.get(key))
        if parsed:
            return parsed, key

    return None, "unresolved"


def apply_publish_date_update(
    mongo_repo,
    doc: dict[str, Any],
    new_date: datetime,
    *,
    dry_run: bool,
) -> bool:
    serialized = serialize_publish_date(new_date)
    if doc_publish_day(doc) == publish_date_day(serialized):
        return False

    if dry_run:
        logger.info(
            "dry-run 更新: %s | %s -> %s",
            (doc.get("title") or "")[:50],
            doc_publish_day(doc) or "—",
            publish_date_day(serialized),
        )
        return True

    now = datetime.now(timezone.utc)
    update_doc = {
        "publish_date": serialized,
        "published_at": serialized,
        "updated_at": now,
    }
    filt = {"content_hash": doc["content_hash"]} if doc.get("content_hash") else {"_id": doc["_id"]}
    result = mongo_repo._notices.update_one(filt, {"$set": update_doc})
    if doc.get("is_bim_related") is True and mongo_repo._bim_notices is not None:
        mongo_repo._bim_notices.update_one(filt, {"$set": update_doc})
    return bool(result.modified_count)


async def run_backfill_async(
    *,
    limit: int = 0,
    bim_only: bool = True,
    source: str | None = None,
    only_missing: bool = True,
    fix_suspicious: bool = False,
    force: bool = False,
    dry_run: bool = False,
    max_index_pages: int = 10,
    fetch_get_info_api: bool = True,
    rate_limit: float = 0.2,
) -> dict[str, Any]:
    settings = get_settings()
    mongo_repo = create_mongo_repository(
        uri=settings.mongodb_uri,
        db_name=settings.mongodb_db,
        collection_name=settings.mongodb_collection,
    )
    if not mongo_repo or not mongo_repo.available or mongo_repo._notices is None:
        logger.error("MongoDB 不可用")
        return {"ok": False, "dry_run": dry_run}

    query = build_query(
        bim_only=bim_only,
        source=source,
        only_missing=only_missing and not fix_suspicious,
        force=force,
    )
    total_matched = mongo_repo._notices.count_documents(query)
    logger.info("查询命中 %d 条（bim_only=%s source=%r）", total_matched, bim_only, source)

    id_publish_index: dict[str, datetime] = {}
    if fetch_get_info_api or source in (None, POWERCHINA_SITE_ID):
        logger.info("构建电建 id→publishTime 索引（max_pages=%d）...", max_index_pages)
        id_publish_index = await build_id_publish_index(
            keywords=["BIM"],
            max_pages=max(1, max_index_pages),
            page_size=100,
            rate_limit_seconds=max(0.0, rate_limit),
        )
        logger.info("索引条目: %d", len(id_publish_index))

    attempted = updated = skipped = unchanged = 0
    reasons: dict[str, int] = {}
    get_info_cache: dict[str, datetime | None] = {}

    cursor = mongo_repo._notices.find(query).sort([("publish_date", 1), ("_id", 1)])
    for doc in cursor:
        if not should_process_doc(
            doc,
            only_missing=only_missing and not force,
            fix_suspicious=fix_suspicious,
            force=force,
        ):
            continue
        if limit > 0 and attempted >= limit:
            break
        attempted += 1

        new_date, source_reason = await resolve_publish_date(
            doc,
            id_publish_index=id_publish_index,
            fetch_get_info_api=fetch_get_info_api,
            get_info_cache=get_info_cache,
        )

        content_date = extract_publish_date_from_content(doc.get("content_text"))
        current_day = doc_publish_day(doc)
        if content_date:
            if not new_date:
                new_date = content_date
                source_reason = "content_text"
            elif source_reason not in ("allList_index", "getInfo") and (
                not current_day or publish_date_day(content_date) != current_day
            ):
                new_date = content_date
                source_reason = "content_text"

        if not new_date:
            skipped += 1
            reasons["unresolved"] = reasons.get("unresolved", 0) + 1
            logger.warning(
                "无法解析发布时间: %s | %s",
                (doc.get("title") or "")[:50],
                _doc_url(doc)[:70],
            )
            continue

        if apply_publish_date_update(mongo_repo, doc, new_date, dry_run=dry_run):
            updated += 1
            reasons[source_reason] = reasons.get(source_reason, 0) + 1
        else:
            unchanged += 1
            reasons["unchanged"] = reasons.get("unchanged", 0) + 1

        if attempted % 20 == 0:
            logger.info("进度: 处理 %d / 更新 %d / 跳过 %d", attempted, updated, skipped)

    mongo_repo.close()
    return {
        "ok": True,
        "dry_run": dry_run,
        "query_total": total_matched,
        "attempted": attempted,
        "updated": updated,
        "unchanged": unchanged,
        "skipped": skipped,
        "reasons": reasons,
        "index_size": len(id_publish_index),
    }


def run_backfill(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(run_backfill_async(**kwargs))


def main() -> int:
    parser = argparse.ArgumentParser(description="回填公告 publish_date")
    parser.add_argument("--limit", type=int, default=0, help="最多处理条数（0=不限）")
    parser.add_argument("--bim-only", action="store_true", default=True)
    parser.add_argument("--all-notices", action="store_true", help="处理全部 MVP 公告")
    parser.add_argument("--source", help="限定站点 source_site_id")
    parser.add_argument("--only-missing", action="store_true", default=True)
    parser.add_argument("--fix-suspicious", action="store_true", help="含缺失或与采集日相同的记录")
    parser.add_argument("--force", action="store_true", help="强制重解析全部命中记录")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-index-pages", type=int, default=10)
    parser.add_argument("--no-getinfo", action="store_true", help="不调用 getInfo，仅用列表索引/正文")
    parser.add_argument("--rate-limit", type=float, default=0.2)
    parser.add_argument("--log", type=Path)
    args = parser.parse_args()

    bim_only = not args.all_notices
    only_missing = args.only_missing and not args.force and not args.fix_suspicious

    ts = datetime.now(TZ_CN).strftime("%Y%m%d_%H%M%S")
    log_path = None if args.dry_run else (
        args.log or (ROOT / "data" / f"backfill_publish_date_{ts}.log")
    )
    setup_logging(log_path)

    stats = run_backfill(
        limit=args.limit,
        bim_only=bim_only,
        source=args.source,
        only_missing=only_missing,
        fix_suspicious=args.fix_suspicious,
        force=args.force,
        dry_run=args.dry_run,
        max_index_pages=max(1, args.max_index_pages),
        fetch_get_info_api=not args.no_getinfo,
        rate_limit=max(0.0, args.rate_limit),
    )

    logger.info(
        "%s完成: 命中 %d | 处理 %d | 更新 %d | 未变 %d | 跳过 %d",
        "dry-run " if args.dry_run else "",
        stats.get("query_total", 0),
        stats.get("attempted", 0),
        stats.get("updated", 0),
        stats.get("unchanged", 0),
        stats.get("skipped", 0),
    )
    if stats.get("reasons"):
        logger.info("来源分布: %s", stats["reasons"])
    if log_path:
        logger.info("日志: %s", log_path)
    return 0 if stats.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
