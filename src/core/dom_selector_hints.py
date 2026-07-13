"""从 HTML 启发式推断列表页选择器，供 crawl_rules 生成使用。"""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urljoin

_LINK_RE = re.compile(
    r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]{2,200})</a>',
    re.I,
)
_TABLE_RE = re.compile(r"<table[^>]*>(.*?)</table>", re.I | re.S)
_UL_RE = re.compile(r"<ul[^>]*>(.*?)</ul>", re.I | re.S)
_LI_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.I | re.S)
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.S)
_CLASS_ID_RE = re.compile(r'\b(?:class|id)=["\']([^"\']+)["\']', re.I)


def _first_class_or_tag(open_tag: str, default_tag: str) -> str:
    m = _CLASS_ID_RE.search(open_tag or "")
    if not m:
        return default_tag
    raw = m.group(1).strip().split()[0]
    if "id" in (open_tag or "").lower()[:20]:
        return f"#{raw}"
    return f".{raw}" if raw and not raw.startswith(".") else raw or default_tag


def _count_links_in_html(chunk: str) -> int:
    return len(_LINK_RE.findall(chunk))


def _extract_link_samples(chunk: str, base_url: str = "", limit: int = 5) -> list[dict[str, str]]:
    samples: list[dict[str, str]] = []
    for href, text in _LINK_RE.findall(chunk):
        text = re.sub(r"\s+", " ", text).strip()
        if not text or href.startswith("javascript:"):
            continue
        url = urljoin(base_url, href) if base_url else href
        samples.append({"title": text[:80], "url": url[:200]})
        if len(samples) >= limit:
            break
    return samples


def analyze_list_dom(html: str, *, url: str = "", max_chars: int = 120_000) -> dict[str, Any]:
    """分析页面 DOM，返回候选 list_page 选择器与 LLM page_hints 文本。"""
    text = (html or "")[:max_chars]
    if not text.strip():
        return {
            "ok": False,
            "error": "HTML 为空",
            "selectors": {},
            "page_hints": "",
            "sample_items": [],
        }

    candidates: list[dict[str, Any]] = []

    for m in _TABLE_RE.finditer(text):
        block = m.group(0)
        link_count = _count_links_in_html(block)
        if link_count < 3:
            continue
        trs = _TR_RE.findall(block)
        if len(trs) < 2:
            continue
        candidates.append(
            {
                "score": link_count + len(trs),
                "container": "table",
                "item": "tr",
                "title": "a",
                "link": "a",
                "date": "td:last-child, .date, span.date",
                "strategy": "dom",
                "sample_items": _extract_link_samples(block, url),
            }
        )

    for m in _UL_RE.finditer(text):
        block = m.group(0)
        lis = _LI_RE.findall(block)
        link_count = _count_links_in_html(block)
        if link_count < 3 or len(lis) < 3:
            continue
        open_ul = m.group(0)[:80]
        container = _first_class_or_tag(open_ul, "ul")
        candidates.append(
            {
                "score": link_count + len(lis),
                "container": container,
                "item": "li",
                "title": "a",
                "link": "a",
                "date": "span.date, .date, time",
                "strategy": "dom",
                "sample_items": _extract_link_samples(block, url),
            }
        )

    # 常见列表 class
    for pattern, container, item in (
        (r'<div[^>]*class=["\'][^"\']*list[^"\']*["\'][^>]*>', ".list", "li, .item, tr"),
        (r'<div[^>]*class=["\'][^"\']*news[^"\']*["\'][^>]*>', ".news-list, .news", "li, .item"),
        (r'<div[^>]*id=["\']noticeShow["\']', "#noticeShow", "li"),
    ):
        if re.search(pattern, text, re.I):
            candidates.append(
                {
                    "score": 20,
                    "container": container.split(",")[0].strip(),
                    "item": item.split(",")[0].strip(),
                    "title": "a",
                    "link": "a",
                    "date": "span.date, .date",
                    "strategy": "dom",
                    "sample_items": _extract_link_samples(text, url, limit=3),
                }
            )

    title_m = re.search(r"<title[^>]*>([^<]+)</title>", text, re.I)
    page_title = title_m.group(1).strip() if title_m else ""
    ajax_hints = list(dict.fromkeys(re.findall(r"[\w/]+\.(?:do|json|api)[\w/?=&.-]*", text)))[:12]

    best: Optional[dict[str, Any]] = None
    if candidates:
        best = max(candidates, key=lambda c: c.get("score", 0))

    selectors: dict[str, str] = {}
    sample_items: list[dict[str, str]] = []
    if best:
        selectors = {
            "container": best["container"],
            "item": best["item"],
            "title": best["title"],
            "link": best["link"],
            "date": best.get("date", ""),
            "strategy": best.get("strategy", "dom"),
        }
        sample_items = best.get("sample_items") or []

    hint_lines = [f"页面标题: {page_title}"]
    if url:
        hint_lines.append(f"页面 URL: {url}")
    if selectors:
        hint_lines.append("推断 list_page 选择器:")
        for key in ("strategy", "container", "item", "title", "link", "date"):
            if selectors.get(key):
                hint_lines.append(f"  {key}: {selectors[key]}")
    if sample_items:
        hint_lines.append(f"链接样本 ({len(sample_items)} 条):")
        for item in sample_items[:5]:
            hint_lines.append(f"  - {item.get('title', '')[:50]} → {item.get('url', '')[:120]}")
    if ajax_hints:
        hint_lines.append(f"疑似 API/XHR: {', '.join(ajax_hints[:8])}")
        if not selectors.get("strategy") or selectors["strategy"] == "dom":
            hint_lines.append("提示: 若列表由 XHR 加载，考虑 list_page.strategy=api 或 dom_after_ajax")

    return {
        "ok": bool(selectors),
        "page_title": page_title,
        "selectors": selectors,
        "page_hints": "\n".join(hint_lines),
        "sample_items": sample_items,
        "api_hints": ajax_hints,
        "candidate_count": len(candidates),
    }


def format_page_hints_for_llm(
    analysis: dict[str, Any],
    *,
    extra_notes: str = "",
) -> str:
    """合并 DOM 分析与用户备注，供 RuleGenerator messages 使用。"""
    parts: list[str] = []
    hints = (analysis.get("page_hints") or "").strip()
    if hints:
        parts.append(hints)
    notes = (extra_notes or "").strip()
    if notes:
        parts.append(f"探查备注:\n{notes}")
    return "\n\n".join(parts)
