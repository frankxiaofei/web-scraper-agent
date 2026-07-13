#!/usr/bin/env python3
"""为 bid.powerchina.cn BIM 公告补抓正文（修正错误 ID / 搜索占位 URL）。"""

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

from scripts.backfill_bim_content import (  # noqa: E402
    build_bim_content_query,
    get_content_stats,
    is_incomplete_content,
    setup_logging,
)
from src.core.config import get_settings  # noqa: E402
from src.core.models import BidNotice, NoticeCategory  # noqa: E402
from src.core.powerchina_notice import (  # noqa: E402
    POWERCHINA_SITE_ID,
    build_detail_url,
    build_title_index,
    fetch_notice_content,
    is_search_placeholder_url,
    resolve_notice_id,
)
from src.db.mongo_repository import MongoRepository, create_mongo_repository  # noqa: E402

TZ_CN = timezone(timedelta(hours=8))
logger = logging.getLogger("backfill_powerchina_bim_content")
DEFAULT_BATCH_SIZE = 20


def _doc_source_id(doc: dict[str, Any]) -> str:
    return doc.get("source_site_id") or doc.get("source") or doc.get("site_id") or ""


def _doc_url(doc: dict[str, Any]) -> str:
    return doc.get("url") or doc.get("detail_url") or ""


def iter_powerchina_bim_docs(
    col,
    *,
    limit: int,
    force: bool,
) -> list[dict[str, Any]]:
    query = build_bim_content_query([POWERCHINA_SITE_ID], source=POWERCHINA_SITE_ID)
    docs: list[dict[str, Any]] = []
    cursor = col.find(query).sort([("crawled_at", -1), ("scraped_at", -1), ("_id", -1)])
    for doc in cursor:
        if not force:
            bad, _ = is_incomplete_content(doc)
            if not bad:
                continue
        docs.append(doc)
        if limit > 0 and len(docs) >= limit:
            break
    return docs


async def _update_doc(
    mongo_repo: MongoRepository,
    doc: dict[str, Any],
    *,
    notice_id: str,
    content_text: str,
    content_html: str,
    api_title: str,
    dry_run: bool,
) -> bool:
    detail_url = build_detail_url(notice_id)
    if dry_run:
        logger.info(
            "dry-run 将更新: %s | id=%s | text=%d",
            (doc.get("title") or "")[:50],
            notice_id,
            len(content_text),
        )
        return True

    pub_dt = None
    pub_raw = doc.get("publish_date") or doc.get("published_at")
    if pub_raw:
        from src.core.publish_date_utils import parse_publish_datetime

        pub_dt = parse_publish_datetime(pub_raw)

    enriched = BidNotice(
        title=api_title or doc.get("title") or "",
        url=detail_url,
        source_site_id=_doc_source_id(doc) or POWERCHINA_SITE_ID,
        source_site_name=doc.get("source_site_name") or POWERCHINA_SITE_ID,
        source_url=doc.get("source_url") or "https://bid.powerchina.cn",
        content_hash=doc.get("content_hash"),
        category=NoticeCategory.TENDER,
        publish_date=pub_dt,
        content_text=content_text or None,
        content_html=content_html or None,
        is_bim_related=doc.get("is_bim_related"),
        bim_confidence=doc.get("bim_confidence"),
        bim_reason=doc.get("bim_reason"),
        bim_tags=doc.get("bim_tags"),
        bim_classified_at=doc.get("bim_classified_at"),
    )
    return bool(mongo_repo.upsert_notice(enriched))


async def run_backfill_async(
    *,
    limit: int = 100,
    force: bool = False,
    dry_run: bool = False,
    max_index_pages: int = 500,
    force_index_refresh: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
    settings = get_settings()
    mongo_repo = create_mongo_repository(
        uri=settings.mongodb_uri,
        db_name=settings.mongodb_db,
        collection_name=settings.mongodb_collection,
    )
    if not mongo_repo or not mongo_repo.available or mongo_repo._notices is None:
        logger.error("MongoDB 不可用，请先 make start-mongo")
        return {"ok": False, "dry_run": dry_run}

    stats_before = get_content_stats(mongo_repo, source=POWERCHINA_SITE_ID)
    logger.info(
        "电建 BIM 正文: 总计 %d | 不全 %d | 完整 %d",
        stats_before.get("bim_total", 0),
        stats_before.get("incomplete", 0),
        stats_before.get("complete", 0),
    )

    docs = iter_powerchina_bim_docs(mongo_repo._notices, limit=limit, force=force)
    placeholder_count = sum(1 for d in docs if is_search_placeholder_url(_doc_url(d)))
    logger.info(
        "待处理 %d 条（搜索占位 URL %d 条）",
        len(docs),
        placeholder_count,
    )

    if not docs:
        mongo_repo.close()
        return {
            "ok": True,
            "dry_run": dry_run,
            "attempted": 0,
            "updated": 0,
            "skipped": 0,
            "before": stats_before,
            "after": stats_before,
        }

    target_titles = {str(d.get("title") or "").strip() for d in docs if d.get("title")}
    logger.info("构建 title→id 索引（max_pages=%d）...", max_index_pages)
    title_index = await build_title_index(
        target_titles,
        max_pages=max_index_pages,
        force_refresh=force_index_refresh,
    )
    logger.info("索引条目: %d", len(title_index))

    attempted = updated = skipped = 0
    reasons: dict[str, int] = {}

    for doc in docs:
        attempted += 1
        title = str(doc.get("title") or "").strip()
        url = _doc_url(doc)
        if not title:
            skipped += 1
            reasons["empty_title"] = reasons.get("empty_title", 0) + 1
            continue

        notice_id, resolve_reason = await resolve_notice_id(title, url, title_index)
        if not notice_id:
            logger.warning("无法解析 ID: %s", title[:60])
            skipped += 1
            reasons["unresolved_id"] = reasons.get("unresolved_id", 0) + 1
            continue

        fetched = await fetch_notice_content(
            notice_id=notice_id,
            expected_title=title,
            verify_title=True,
        )
        if not fetched.get("ok"):
            logger.warning(
                "正文抓取失败: id=%s | %s | %s",
                notice_id,
                title[:60],
                fetched.get("error"),
            )
            skipped += 1
            reasons["fetch_failed"] = reasons.get("fetch_failed", 0) + 1
            continue

        content_text = fetched.get("content_text") or ""
        content_html = fetched.get("content_html") or ""
        content_source = fetched.get("content_source") or "empty"
        if not content_text and not content_html:
            logger.warning(
                "getInfo/PDF 无正文，仍修正 URL: id=%s | %s | source=%s",
                notice_id,
                title[:60],
                content_source,
            )
            ok = await _update_doc(
                mongo_repo,
                doc,
                notice_id=notice_id,
                content_text="",
                content_html="",
                api_title=fetched.get("title") or title,
                dry_run=dry_run,
            )
            if ok:
                updated += 1
                reasons["url_only_fix"] = reasons.get("url_only_fix", 0) + 1
            else:
                skipped += 1
                reasons["upsert_failed"] = reasons.get("upsert_failed", 0) + 1
            continue

        ok = await _update_doc(
            mongo_repo,
            doc,
            notice_id=notice_id,
            content_text=content_text,
            content_html=content_html,
            api_title=fetched.get("title") or title,
            dry_run=dry_run,
        )
        if ok:
            updated += 1
            reason_key = resolve_reason if content_source == "html" else content_source
            reasons[reason_key] = reasons.get(reason_key, 0) + 1
            if not dry_run:
                preview = content_text[:60].replace("\n", " ")
                logger.info(
                    "✓ id=%s | %s | [%s] %s...",
                    notice_id,
                    title[:45],
                    content_source,
                    preview,
                )
        else:
            skipped += 1
            reasons["upsert_failed"] = reasons.get("upsert_failed", 0) + 1

        if attempted % batch_size == 0:
            logger.info("进度: 处理 %d / 更新 %d / 跳过 %d", attempted, updated, skipped)

    stats_after = get_content_stats(mongo_repo, source=POWERCHINA_SITE_ID)
    mongo_repo.close()
    return {
        "ok": True,
        "dry_run": dry_run,
        "attempted": attempted,
        "updated": updated,
        "skipped": skipped,
        "reasons": reasons,
        "before": stats_before,
        "after": stats_after,
        "index_size": len(title_index),
        "pending": len(docs),
    }


def run_backfill(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(run_backfill_async(**kwargs))


def main() -> int:
    parser = argparse.ArgumentParser(description="电建 BIM 公告正文补抓（修正 ID 映射）")
    parser.add_argument("--limit", type=int, default=100, help="最多处理条数（0=不限）")
    parser.add_argument("--force", action="store_true", help="强制重抓所有 BIM 正文")
    parser.add_argument("--dry-run", action="store_true", help="仅统计与模拟，不写 Mongo")
    parser.add_argument(
        "--max-index-pages",
        type=int,
        default=10,
        help="allList keyWords/curpage 扫描最大页数（BIM 约 4 页即可）",
    )
    parser.add_argument(
        "--force-index-refresh",
        action="store_true",
        help="忽略 data/powerchina_bim_title_index.json 缓存",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--log", type=Path)
    args = parser.parse_args()

    ts = datetime.now(TZ_CN).strftime("%Y%m%d_%H%M%S")
    log_path = None if args.dry_run else (
        args.log or (ROOT / "data" / f"backfill_powerchina_bim_content_{ts}.log")
    )
    setup_logging(log_path)

    stats = run_backfill(
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
        max_index_pages=max(1, args.max_index_pages),
        force_index_refresh=args.force_index_refresh,
        batch_size=max(1, args.batch_size),
    )

    before = stats.get("before") or {}
    after = stats.get("after") or {}
    if args.dry_run:
        logger.info(
            "dry-run: 待处理 %d | 索引 %d | 将更新 %d | 跳过 %d",
            stats.get("pending", 0),
            stats.get("index_size", 0),
            stats.get("updated", 0),
            stats.get("skipped", 0),
        )
    else:
        logger.info(
            "完成: 处理 %d | 更新 %d | 跳过 %d | 不全 %d -> %d",
            stats.get("attempted", 0),
            stats.get("updated", 0),
            stats.get("skipped", 0),
            before.get("incomplete", "—"),
            after.get("incomplete", "—"),
        )
        if stats.get("reasons"):
            logger.info("原因分布: %s", stats["reasons"])
        if log_path:
            logger.info("日志: %s", log_path)
    return 0 if stats.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
