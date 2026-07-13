"""浏览器控制页 — WebBridge 自动列表提取与入库。"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from src.core.crawl_rules import CrawlRule, build_page_number_url, load_crawl_rule
from src.core.site_aliases import resolve_canonical_site_id, site_not_registered_error
from src.core.site_sync import get_site_by_id, load_sites_config
from src.web.crawl_agent_skills import save_notice
from src.web.webbridge_skills import (
    webbridge_click,
    webbridge_extract_list,
    webbridge_extract_links_generic,
    webbridge_navigate,
    webbridge_snapshot,
    webbridge_wait,
)

logger = logging.getLogger(__name__)

BROWSER_CRAWL_MAX_PAGES_CAP = 20
BROWSER_CRAWL_MIN_INTERVAL_SECONDS = 2.0
BROWSER_CRAWL_MAX_ITEMS_PER_PAGE = 200

_last_crawl_at: dict[str, float] = {}


def check_browser_crawl_rate_limit(key: str) -> Optional[str]:
    """同一 session 最短间隔，防止连点。"""
    now = time.time()
    last = _last_crawl_at.get(key, 0.0)
    elapsed = now - last
    if elapsed < BROWSER_CRAWL_MIN_INTERVAL_SECONDS:
        wait = BROWSER_CRAWL_MIN_INTERVAL_SECONDS - elapsed
        return f"请求过于频繁，请 {wait:.1f}s 后重试"
    _last_crawl_at[key] = now
    return None


def infer_site_id_from_url(page_url: str) -> Optional[str]:
    """从当前页 URL 或裸域名匹配 sites.yaml 中站点（最长 host 匹配）。"""
    raw = (page_url or "").strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        if "://" not in raw:
            raw = "https://" + raw.lstrip("/")
    host = urlparse(raw).netloc.lower()
    if not host:
        return None

    best_id: Optional[str] = None
    best_score = 0
    for site in load_sites_config().get("sites") or []:
        site_url = (site.get("url") or "").strip()
        sid = site.get("id")
        if not site_url or not sid:
            continue
        shost = urlparse(site_url).netloc.lower()
        if not shost:
            continue
        if host == shost or host.endswith("." + shost) or shost in host:
            score = len(shost)
            if score > best_score:
                best_score = score
                best_id = sid
    return resolve_canonical_site_id(best_id) if best_id else None


def _resolve_effective_max_pages(rule: Optional[CrawlRule], requested: Optional[int]) -> int:
    rule_cap = BROWSER_CRAWL_MAX_PAGES_CAP
    if rule and rule.limits:
        rule_cap = min(rule_cap, int(rule.limits.max_pages or BROWSER_CRAWL_MAX_PAGES_CAP))
        pag = rule.pagination
        if pag and pag.max_pages:
            rule_cap = min(rule_cap, int(pag.max_pages))
    if requested is None:
        return min(3, rule_cap)
    return max(1, min(int(requested), rule_cap))


def _rate_limit_seconds(rule: Optional[CrawlRule]) -> float:
    if rule and rule.limits and rule.limits.rate_limit_seconds:
        return max(0.0, float(rule.limits.rate_limit_seconds))
    return 0.0


def _list_extract_params(rule: Optional[CrawlRule]) -> tuple[Optional[str], Optional[str], str]:
    """返回 (selector, hint, mode) — mode 为 rule|generic。"""
    if not rule or not rule.list_page:
        return None, None, "generic"
    lp = rule.list_page
    parts: list[str] = []
    if lp.container:
        parts.append(lp.container.strip())
    if lp.item:
        parts.append(lp.item.strip())
    link = (lp.link or lp.title or "").strip()
    if link:
        parts.append(link)
        return " ".join(parts), None, "rule"
    if parts:
        return " ".join(parts), None, "rule"
    wait_for = (lp.wait_for or "").strip()
    hint: Optional[str] = None
    if wait_for:
        m = re.search(r"href\*=['\"]([^'\"]+)", wait_for)
        if m:
            hint = m.group(1)
        elif "zb" in wait_for.lower():
            hint = "zb"
    if hint:
        return None, hint, "rule"
    return None, None, "generic"


def _extract_items(
    *,
    session_id: str,
    rule: Optional[CrawlRule],
    max_items: int,
) -> dict[str, Any]:
    selector, hint, mode = _list_extract_params(rule)
    limit = max(1, min(int(max_items), BROWSER_CRAWL_MAX_ITEMS_PER_PAGE))
    if mode == "generic":
        result = webbridge_extract_links_generic(session_id=session_id, max_items=limit)
        result["extraction_mode"] = "generic"
        return result
    result = webbridge_extract_list(
        selector=selector,
        hint=hint,
        session_id=session_id,
        max_items=limit,
    )
    result["extraction_mode"] = "rule"
    result["selector"] = selector
    return result


def _webbridge_paginate(
    *,
    site_id: str,
    rule: CrawlRule,
    session_id: str,
    page_num: int,
    current_url: str,
    max_pages: int,
) -> dict[str, Any]:
    pag = rule.pagination
    if pag is None:
        return {"ok": True, "has_next": False, "page_num": page_num, "url": current_url}

    if pag.type == "page_number":
        next_num = page_num + 1
        if next_num > max_pages:
            return {"ok": True, "has_next": False, "page_num": page_num, "url": current_url}
        base = current_url or rule.entry_url or ""
        next_url = build_page_number_url(
            base if page_num <= 1 else current_url,
            next_num,
            page_param=pag.page_param,
            url_style=pag.url_style,
        )
        nav = webbridge_navigate(next_url, session_id=session_id, new_tab=False)
        if not nav.get("ok"):
            return {"ok": False, "error": nav.get("error") or "翻页导航失败", "page_num": page_num}
        wait_s = max(0.0, (pag.wait_after_ms or 0) / 1000.0)
        if wait_s > 0:
            webbridge_wait(min(wait_s, 30.0), session_id=session_id)
        return {
            "ok": True,
            "has_next": next_num < max_pages,
            "page_num": next_num,
            "url": nav.get("url") or next_url,
            "next_page_url": next_url if next_num < max_pages else None,
            "pagination_strategy": "page_number",
        }

    if pag.type == "next_button":
        if page_num >= max_pages:
            return {"ok": True, "has_next": False, "page_num": page_num, "url": current_url}
        selector = (pag.selector or "").strip()
        if not selector:
            return {"ok": False, "error": "分页规则缺少 selector", "page_num": page_num}
        click = webbridge_click(selector, session_id=session_id)
        if not click.get("ok"):
            return {"ok": False, "error": click.get("error") or "点击下一页失败", "page_num": page_num}
        wait_s = max(0.5, (pag.wait_after_ms or 500) / 1000.0)
        webbridge_wait(min(wait_s, 30.0), session_id=session_id)
        snap = webbridge_snapshot(session_id=session_id)
        new_url = snap.get("url") or current_url
        next_num = page_num + 1
        return {
            "ok": True,
            "has_next": next_num < max_pages,
            "page_num": next_num,
            "url": new_url,
            "pagination_strategy": "next_button",
        }

    return {
        "ok": False,
        "error": f"WebBridge 自动爬取暂不支持分页策略: {pag.type}",
        "page_num": page_num,
    }


def browser_auto_crawl(
    *,
    session_id: str,
    site_id: str,
    max_pages: Optional[int] = None,
    save: bool = True,
    confirm_generic: bool = False,
    on_progress: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    """snapshot → extract_list → 可选翻页 → save_notice 批量入库。"""
    sid = (session_id or "").strip() or "browser-ui"
    canonical_id = resolve_canonical_site_id((site_id or "").strip())
    if not canonical_id:
        return {"ok": False, "error": "site_id 必填"}

    site = get_site_by_id(canonical_id)
    if not site:
        return {"ok": False, "error": site_not_registered_error(canonical_id), "site_id": canonical_id}

    rate_err = check_browser_crawl_rate_limit(sid)
    if rate_err:
        return {"ok": False, "error": rate_err, "site_id": canonical_id}

    rule = load_crawl_rule(site)
    effective_max_pages = _resolve_effective_max_pages(rule, max_pages)
    rate_limit = _rate_limit_seconds(rule)

    def progress(step: str, **extra: Any) -> None:
        payload = {"step": step, "site_id": canonical_id, **extra}
        if on_progress:
            on_progress(payload)

    progress("snapshot")
    snap = webbridge_snapshot(session_id=sid)
    if not snap.get("ok"):
        return {
            "ok": False,
            "error": snap.get("error") or "snapshot 失败",
            "site_id": canonical_id,
            "steps": [{"step": "snapshot", "ok": False, "error": snap.get("error")}],
        }

    page_url = snap.get("url") or ""
    page_title = snap.get("title") or ""
    steps: list[dict[str, Any]] = [
        {"step": "snapshot", "ok": True, "url": page_url, "title": page_title},
    ]

    all_items: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    save_results: list[dict[str, Any]] = []
    errors: list[str] = []
    page_num = 1
    current_url = page_url
    extraction_mode = "rule"

    while page_num <= effective_max_pages:
        progress("extract", page_num=page_num, url=current_url)
        extract = _extract_items(session_id=sid, rule=rule, max_items=BROWSER_CRAWL_MAX_ITEMS_PER_PAGE)
        if not extract.get("ok"):
            err = extract.get("error") or "列表提取失败"
            errors.append(f"第{page_num}页: {err}")
            steps.append({"step": "extract", "page_num": page_num, "ok": False, "error": err})
            break

        extraction_mode = extract.get("extraction_mode") or extraction_mode
        batch = extract.get("items") or []
        new_count = 0
        for item in batch:
            url = (item.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            all_items.append({"title": item.get("title") or url, "url": url})
            new_count += 1

        steps.append(
            {
                "step": "extract",
                "page_num": page_num,
                "ok": True,
                "item_count": len(batch),
                "new_unique": new_count,
                "extraction_mode": extraction_mode,
                "selector": extract.get("selector"),
            }
        )

        if page_num >= effective_max_pages:
            break

        if not rule or not rule.pagination:
            break

        progress("paginate", page_num=page_num)
        if rate_limit > 0:
            time.sleep(rate_limit)
        page_result = _webbridge_paginate(
            site_id=canonical_id,
            rule=rule,
            session_id=sid,
            page_num=page_num,
            current_url=current_url,
            max_pages=effective_max_pages,
        )
        if not page_result.get("ok"):
            err = page_result.get("error") or "翻页失败"
            errors.append(f"第{page_num}页后: {err}")
            steps.append({"step": "paginate", "page_num": page_num, "ok": False, "error": err})
            break
        steps.append(
            {
                "step": "paginate",
                "page_num": page_num,
                "ok": True,
                "has_next": page_result.get("has_next"),
                "strategy": page_result.get("pagination_strategy"),
            }
        )
        if not page_result.get("has_next"):
            break
        page_num = int(page_result.get("page_num") or page_num + 1)
        current_url = page_result.get("url") or current_url

    needs_confirmation = extraction_mode == "generic" and not confirm_generic
    saved_count = 0
    duplicate_count = 0
    skipped_save = False

    if save and all_items:
        if needs_confirmation:
            skipped_save = True
            steps.append(
                {
                    "step": "save",
                    "ok": True,
                    "skipped": True,
                    "reason": "generic 提取需用户确认后再入库",
                }
            )
        else:
            progress("save", total=len(all_items))
            for idx, item in enumerate(all_items, start=1):
                if rate_limit > 0 and idx > 1:
                    time.sleep(min(rate_limit, 1.0))
                try:
                    result = save_notice(
                        canonical_id,
                        title=item["title"],
                        url=item["url"],
                        index=idx,
                    )
                except Exception as exc:
                    logger.exception("browser crawl save_notice 失败")
                    errors.append(f"保存 {item['url'][:60]}: {exc}")
                    save_results.append({"url": item["url"], "ok": False, "error": str(exc)})
                    continue
                save_results.append(result)
                if result.get("saved"):
                    saved_count += 1
                elif result.get("duplicate"):
                    duplicate_count += 1
            steps.append(
                {
                    "step": "save",
                    "ok": True,
                    "saved_count": saved_count,
                    "duplicate_count": duplicate_count,
                    "total": len(all_items),
                }
            )

    progress("done")
    return {
        "ok": True,
        "site_id": canonical_id,
        "session_id": sid,
        "page_url": page_url,
        "page_title": page_title,
        "extraction_mode": extraction_mode,
        "needs_confirmation": needs_confirmation,
        "max_pages": effective_max_pages,
        "pages_crawled": page_num,
        "item_count": len(all_items),
        "items": all_items[:50],
        "items_truncated": len(all_items) > 50,
        "saved_count": saved_count,
        "duplicate_count": duplicate_count,
        "skipped_save": skipped_save,
        "save_enabled": save,
        "errors": errors,
        "steps": steps,
        "save_results": save_results[:20] if save_results else [],
    }
