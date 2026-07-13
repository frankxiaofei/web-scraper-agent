"""股票专题监测配置与关键词匹配。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
STOCK_WATCH_PATH = ROOT / "config" / "stock_watch.yaml"

DEFAULT_MATCH_KEYWORDS: tuple[str, ...] = (
    "股票",
    "证券",
    "上市公司",
    "董事会",
    "股东大会",
    "信息披露",
    "财报",
    "定增",
    "并购",
    "股权激励",
    "A股",
    "港股",
)


@lru_cache(maxsize=1)
def load_stock_watch_config() -> dict[str, Any]:
    if not STOCK_WATCH_PATH.is_file():
        return {
            "scope_label": "招采公告库关键词筛选",
            "match_keywords": list(DEFAULT_MATCH_KEYWORDS),
            "themes": [],
            "watch_symbols": [],
            "use_mock_when_empty": True,
            "mock_items": [],
        }
    with open(STOCK_WATCH_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_stock_scope_label() -> str:
    cfg = load_stock_watch_config()
    return str(cfg.get("scope_label") or "招采公告库关键词筛选")


def get_stock_match_keywords() -> list[str]:
    cfg = load_stock_watch_config()
    kws = cfg.get("match_keywords") or list(DEFAULT_MATCH_KEYWORDS)
    return [str(k).strip() for k in kws if str(k).strip()]


def get_stock_themes() -> list[dict[str, Any]]:
    cfg = load_stock_watch_config()
    themes = cfg.get("themes") or []
    return [dict(t) for t in themes if isinstance(t, dict)]


def get_watch_symbols() -> list[dict[str, Any]]:
    cfg = load_stock_watch_config()
    symbols = cfg.get("watch_symbols") or []
    return [dict(s) for s in symbols if isinstance(s, dict)]


def get_industries() -> list[dict[str, Any]]:
    cfg = load_stock_watch_config()
    industries = cfg.get("industries") or []
    return [dict(i) for i in industries if isinstance(i, dict)]


def matched_industries(doc: dict[str, Any]) -> list[str]:
    explicit = doc.get("industries")
    if isinstance(explicit, list) and explicit:
        return [str(x) for x in explicit if str(x).strip()]

    text = _search_text(doc)
    if not text.strip():
        return []

    found: list[str] = []
    upper = text.upper()
    for ind in get_industries():
        name = str(ind.get("name") or ind.get("id") or "")
        for kw in ind.get("keywords") or []:
            kw_str = str(kw).strip()
            if kw_str and (kw_str.upper() in upper or kw_str in text):
                if name and name not in found:
                    found.append(name)
                break
    return found


def get_mock_items() -> list[dict[str, Any]]:
    cfg = load_stock_watch_config()
    return [dict(m) for m in (cfg.get("mock_items") or []) if isinstance(m, dict)]


def use_mock_when_empty() -> bool:
    cfg = load_stock_watch_config()
    return bool(cfg.get("use_mock_when_empty", True))


def _search_text(doc: dict[str, Any]) -> str:
    parts = [
        doc.get("title") or "",
        doc.get("content_text") or "",
        doc.get("category") or "",
        doc.get("purchaser") or "",
        doc.get("tender_party") or "",
    ]
    return "\n".join(parts)


def matched_stock_keywords(doc: dict[str, Any]) -> list[str]:
    tags = doc.get("stock_tags")
    if isinstance(tags, list) and tags:
        return [str(t) for t in tags if str(t).strip()]
    pre = doc.get("stock_keywords")
    if isinstance(pre, list) and pre:
        return [str(t) for t in pre if str(t).strip()]

    text = _search_text(doc)
    if not text.strip():
        return []

    found: list[str] = []
    upper = text.upper()
    for kw in get_stock_match_keywords():
        if kw.upper() in upper or kw in text:
            found.append(kw)
    for theme in get_stock_themes():
        for kw in theme.get("keywords") or []:
            kw_str = str(kw).strip()
            if kw_str and (kw_str.upper() in upper or kw_str in text) and kw_str not in found:
                found.append(kw_str)
    return found


def matched_stock_themes(doc: dict[str, Any]) -> list[str]:
    explicit = doc.get("theme")
    if explicit:
        return [str(explicit)]

    text = _search_text(doc)
    if not text.strip():
        return []

    themes: list[str] = []
    upper = text.upper()
    for theme in get_stock_themes():
        name = str(theme.get("name") or theme.get("id") or "")
        for kw in theme.get("keywords") or []:
            kw_str = str(kw).strip()
            if kw_str and (kw_str.upper() in upper or kw_str in text):
                if name and name not in themes:
                    themes.append(name)
                break
    return themes


def is_stock_related(doc: dict[str, Any]) -> bool:
    if doc.get("_mock"):
        return True
    if doc.get("stock_related") is True:
        return True
    return bool(matched_stock_keywords(doc))
