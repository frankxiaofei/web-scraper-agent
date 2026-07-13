#!/usr/bin/env python3
"""电建 BIM 专用 HTTP 同步：allList keyWords=BIM + getInfo 详情，不经 WebBridge。

用法:
    python scripts/sync_powerchina_bim_http.py --dry-run
    python scripts/sync_powerchina_bim_http.py --page-size 100 --max-pages 1
    python scripts/sync_powerchina_bim_http.py --keyword BIM --fetch-detail
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_SITE_ID = "中国电力建设集团有限公司_公共资源交易服务平台"
DEFAULT_KEYWORD = "BIM"
TZ_CN = timezone(timedelta(hours=8))
logger = logging.getLogger("sync_powerchina_bim_http")


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


async def _fetch_content_with_retry(
    notice_id: str,
    title: str,
    *,
    max_retries: int,
    rate_limit: float,
) -> dict[str, Any]:
    from src.core.powerchina_notice import fetch_notice_content

    last: dict[str, Any] = {"ok": False, "error": "未尝试"}
    for attempt in range(max(1, max_retries)):
        if attempt > 0 and rate_limit > 0:
            await asyncio.sleep(rate_limit * attempt)
        last = await fetch_notice_content(
            notice_id=notice_id,
            expected_title=title,
            verify_title=True,
        )
        if last.get("ok"):
            return last
        logger.warning(
            "正文抓取失败 id=%s attempt=%d/%d: %s",
            notice_id,
            attempt + 1,
            max_retries,
            last.get("error"),
        )
    return last


async def sync_bim_http(
    site_id: str,
    *,
    keyword: str,
    max_pages: int,
    page_size: int,
    fetch_detail: bool,
    dry_run: bool,
    consult_pages: int = 0,
    max_retries: int = 3,
    rate_limit: float = 0.3,
    batch_size: int = 10,
) -> dict[str, Any]:
    from src.core.powerchina_notice import (
        build_detail_url,
        extract_row_notice_id,
        fetch_consult_list_page,
        fetch_keyword_list_page,
    )
    from src.web.crawl_agent_skills import save_notice

    summary: dict[str, Any] = {
        "ok": True,
        "site_id": site_id,
        "keyword": keyword,
        "dry_run": dry_run,
        "fetch_detail": fetch_detail,
        "saved": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
        "items": [],
        "samples": {"saved": [], "skipped": [], "failed": []},
    }

    page_num = 1
    total = 0
    processed = 0
    while page_num <= max(1, max_pages):
        rows, total = await fetch_keyword_list_page(
            keyword, page_num, page_size=page_size
        )
        logger.info(
            "allList keyWords=%r page=%d pageSize=%d → %d 条 (total=%d)",
            keyword,
            page_num,
            page_size,
            len(rows),
            total,
        )
        if not rows:
            break
        for row in rows:
            title = str(row.get("title") or "").strip()
            notice_id = extract_row_notice_id(row)
            if not title or not notice_id:
                continue
            detail_url = build_detail_url(notice_id)
            pub = str(row.get("publishTime") or row.get("publishDate") or "")[:10] or None

            item_info = {"id": notice_id, "title": title[:120], "url": detail_url}
            summary["items"].append(item_info)
            processed += 1

            if dry_run:
                logger.info("  · [%s] %s", notice_id, title[:56])
                continue

            content_text = content_html = None
            if fetch_detail:
                fetched = await _fetch_content_with_retry(
                    notice_id,
                    title,
                    max_retries=max_retries,
                    rate_limit=rate_limit,
                )
                if fetched.get("ok"):
                    content_text = fetched.get("content_text")
                    content_html = fetched.get("content_html")
                    if fetched.get("title"):
                        title = str(fetched["title"]).strip() or title
                else:
                    summary["failed"] += 1
                    err = f"id={notice_id}: {fetched.get('error') or 'unknown'}"
                    summary["errors"].append(err)
                    if len(summary["samples"]["failed"]) < 5:
                        summary["samples"]["failed"].append(
                            {"id": notice_id, "title": title[:80], "error": fetched.get("error")}
                        )
                    logger.warning("跳过入库（正文失败）: %s | %s", notice_id, title[:50])
                    if rate_limit > 0:
                        await asyncio.sleep(rate_limit)
                    continue

            save_result = save_notice(
                site_id,
                title=title,
                url=detail_url,
                publish_date=pub,
                content_text=content_text,
                content_html=content_html,
            )
            if save_result.get("saved"):
                summary["saved"] += 1
                if len(summary["samples"]["saved"]) < 5:
                    summary["samples"]["saved"].append(
                        {"id": notice_id, "title": title[:80]}
                    )
                src = (content_text or "")[:40].replace("\n", " ")
                logger.info("✓ 入库 id=%s | %s | %s...", notice_id, title[:45], src)
            elif save_result.get("duplicate"):
                summary["skipped"] += 1
                if len(summary["samples"]["skipped"]) < 5:
                    summary["samples"]["skipped"].append(
                        {"id": notice_id, "title": title[:80]}
                    )
                logger.info("— 跳过已存在 id=%s | %s", notice_id, title[:45])
            elif save_result.get("error"):
                summary["failed"] += 1
                summary["errors"].append(save_result["error"])
                if len(summary["samples"]["failed"]) < 5:
                    summary["samples"]["failed"].append(
                        {"id": notice_id, "title": title[:80], "error": save_result["error"]}
                    )

            if processed % batch_size == 0:
                logger.info(
                    "进度 %d/%d | 成功 %d | 跳过 %d | 失败 %d",
                    processed,
                    total,
                    summary["saved"],
                    summary["skipped"],
                    summary["failed"],
                )
            if rate_limit > 0:
                await asyncio.sleep(rate_limit)

        if page_num * page_size >= total:
            break
        page_num += 1
        await asyncio.sleep(rate_limit)

    if consult_pages > 0:
        for p in range(1, consult_pages + 1):
            rows, _ = await fetch_consult_list_page(p)
            bim_rows = [
                r for r in rows if keyword.upper() in str(r.get("title", "")).upper()
            ]
            for row in bim_rows:
                title = str(row.get("title") or "").strip()
                notice_id = extract_row_notice_id(row)
                if not title or not notice_id:
                    continue
                detail_url = build_detail_url(notice_id)
                if dry_run:
                    logger.info("  · consult [%s] %s", notice_id, title[:56])
                    continue
                save_result = save_notice(
                    site_id,
                    title=title,
                    url=detail_url,
                    publish_date=str(row.get("publishTime") or "")[:10] or None,
                )
                if save_result.get("saved"):
                    summary["saved"] += 1
                elif save_result.get("duplicate"):
                    summary["skipped"] += 1

    summary["total_keyword_hits"] = total
    summary["list_count"] = len(summary["items"])
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="电建 BIM 关键词 HTTP 同步")
    parser.add_argument("--site-id", default=DEFAULT_SITE_ID)
    parser.add_argument("--keyword", default=DEFAULT_KEYWORD)
    parser.add_argument("--max-pages", type=int, default=1, help="keyWords 搜索最大页数")
    parser.add_argument("--page-size", type=int, default=100, help="allList pageSize")
    parser.add_argument(
        "--consult-pages",
        type=int,
        default=0,
        help="额外扫描 consult/notice list 最近 N 页并过滤标题含关键词",
    )
    parser.add_argument("--fetch-detail", action="store_true", default=True)
    parser.add_argument("--no-detail", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-retries", type=int, default=3, help="getInfo/PDF 失败重试次数")
    parser.add_argument("--rate-limit", type=float, default=0.3, help="条间间隔秒")
    parser.add_argument("--batch-size", type=int, default=10, help="进度日志间隔")
    parser.add_argument("--log", type=Path, help="日志文件路径")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    ts = datetime.now(TZ_CN).strftime("%Y%m%d_%H%M%S")
    log_path = None if args.dry_run else (
        args.log or (ROOT / "data" / f"sync_powerchina_bim_http_{ts}.log")
    )
    setup_logging(log_path)

    fetch_detail = args.fetch_detail and not args.no_detail
    result = asyncio.run(
        sync_bim_http(
            args.site_id,
            keyword=args.keyword,
            max_pages=max(1, args.max_pages),
            page_size=max(1, args.page_size),
            fetch_detail=fetch_detail,
            dry_run=args.dry_run,
            consult_pages=max(0, args.consult_pages),
            max_retries=max(1, args.max_retries),
            rate_limit=max(0.0, args.rate_limit),
            batch_size=max(1, args.batch_size),
        )
    )

    logger.info("-" * 60)
    logger.info(
        "完成: keyword=%s list=%d total=%d saved=%d skipped=%d failed=%d errors=%d",
        args.keyword,
        result.get("list_count", 0),
        result.get("total_keyword_hits"),
        result.get("saved"),
        result.get("skipped"),
        result.get("failed"),
        len(result.get("errors", [])),
    )
    for label in ("saved", "skipped", "failed"):
        samples = (result.get("samples") or {}).get(label) or []
        if samples:
            logger.info("样例 %s:", label)
            for s in samples:
                logger.info("  [%s] %s", s.get("id"), s.get("title"))
    if log_path:
        logger.info("日志: %s", log_path)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
