#!/usr/bin/env python3
"""电建 bid.powerchina.cn 纯 HTTP 爬取 + 入库（不经过 LLM）。

适用于 list_page.strategy=api 的站点；零 browser，直连 REST 列表/详情 API。
IP 封锁时在 .env 配置 HTTP_PROXY/HTTPS_PROXY 后重试。

用法:
    python scripts/crawl_powerchina_http.py --dry-run
    python scripts/crawl_powerchina_http.py --max-pages 2 --fetch-detail
    python scripts/crawl_powerchina_http.py --max-pages 6 --no-detail
    HTTP_PROXY=http://127.0.0.1:7890 python scripts/crawl_powerchina_http.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_SITE_ID = "中国电力建设集团有限公司_公共资源交易服务平台"


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


async def crawl_http(
    site_id: str,
    *,
    max_pages: int,
    fetch_detail: bool,
    dry_run: bool,
) -> dict[str, Any]:
    from src.core.http_api_client import resolve_http_proxy
    from src.core.site_sync import get_site_by_id
    from src.core.crawl_rules import load_crawl_rule
    from src.web.crawl_agent_skills import (
        http_fetch_list_page_async,
        fetch_detail_page_async,
        save_notice,
        plan_crawl_path,
    )

    site = get_site_by_id(site_id)
    if not site:
        return {"ok": False, "error": f"站点不存在: {site_id}"}

    rule = load_crawl_rule(site)
    if not rule or not rule.list_page or rule.list_page.strategy != "api":
        return {"ok": False, "error": f"站点 {site_id} 非 list_page.strategy=api，请用 sync 或 Agent"}

    plan = plan_crawl_path(site_id, max_pages_override=max_pages)
    if not plan.get("ok"):
        return {"ok": False, "error": plan.get("error") or "路径规划失败"}

    pages = min(max_pages, int(plan.get("agent_max_pages") or max_pages))
    rate_limit = float(plan.get("rate_limit_seconds") or rule.limits.rate_limit_seconds or 0)
    need_detail = fetch_detail and bool(plan.get("fetch_detail"))

    summary: dict[str, Any] = {
        "ok": True,
        "site_id": site_id,
        "dry_run": dry_run,
        "max_pages": pages,
        "fetch_detail": need_detail,
        "proxy": resolve_http_proxy(),
        "saved": 0,
        "skipped": 0,
        "errors": [],
        "pages": [],
    }

    print(f"=== HTTP 爬取 {site_id} ===")
    print(f"页数: {pages}  详情: {need_detail}  代理: {summary['proxy'] or '(无)'}  dry_run: {dry_run}")
    print("-" * 60)

    for page_num in range(1, pages + 1):
        list_result = await http_fetch_list_page_async(site_id, page_num=page_num)
        page_info = {
            "page_num": page_num,
            "ok": list_result.get("ok"),
            "item_count": list_result.get("item_count", 0),
        }
        if not list_result.get("ok"):
            page_info["error"] = list_result.get("error")
            summary["errors"].append(page_info["error"])
            summary["pages"].append(page_info)
            print(f"[页 {page_num}] 失败: {list_result.get('error')}")
            if list_result.get("hint"):
                print(f"  提示: {list_result['hint']}")
            summary["ok"] = False
            break

        items = list_result.get("items") or []
        print(f"[页 {page_num}] {len(items)} 条")
        page_saved = 0
        page_skipped = 0

        for item in items:
            title = item.get("title") or item.get("url") or "?"
            url = item.get("url") or ""
            print(f"  · {title[:56]}")

            if dry_run:
                continue

            detail_payload: dict[str, Any] = {}
            if need_detail and url:
                detail_payload = await fetch_detail_page_async(site_id, url, title=title)
                if not detail_payload.get("ok"):
                    summary["errors"].append(
                        f"详情失败 {url}: {detail_payload.get('error')}"
                    )

            save_args: dict[str, Any] = {
                "title": title,
                "url": url,
                "publish_date": item.get("date"),
            }
            if detail_payload.get("ok"):
                save_args["content_text"] = detail_payload.get("content_text")
                save_args["attachments"] = detail_payload.get("attachments")

            save_result = save_notice(site_id, **save_args)
            if save_result.get("saved"):
                page_saved += 1
                summary["saved"] += 1
            elif save_result.get("duplicate"):
                page_skipped += 1
                summary["skipped"] += 1

        page_info["saved"] = page_saved
        page_info["skipped"] = page_skipped
        summary["pages"].append(page_info)

        if rate_limit > 0 and page_num < pages:
            await asyncio.sleep(rate_limit)

    print("-" * 60)
    print(
        f"完成: saved={summary['saved']} skipped={summary['skipped']} "
        f"errors={len(summary['errors'])}"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="电建 API 站纯 HTTP 爬取（不经 LLM）")
    parser.add_argument(
        "--site-id",
        default=DEFAULT_SITE_ID,
        help=f"站点 ID（默认 {DEFAULT_SITE_ID}）",
    )
    parser.add_argument("--max-pages", type=int, default=3, help="最大列表页数（默认 3）")
    parser.add_argument(
        "--fetch-detail",
        action="store_true",
        default=True,
        help="抓取详情 API 正文（默认开启）",
    )
    parser.add_argument(
        "--no-detail",
        action="store_true",
        help="仅保存列表项，不抓详情",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只拉列表预览，不写 JSONL/Mongo",
    )
    parser.add_argument("--json", action="store_true", help="最后输出 JSON 摘要")
    args = parser.parse_args()

    fetch_detail = args.fetch_detail and not args.no_detail
    try:
        result = asyncio.run(
            crawl_http(
                args.site_id,
                max_pages=max(1, args.max_pages),
                fetch_detail=fetch_detail,
                dry_run=args.dry_run,
            )
        )
    except KeyboardInterrupt:
        print("\n已中断")
        return 130

    if args.json:
        _print_json(result)

    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
