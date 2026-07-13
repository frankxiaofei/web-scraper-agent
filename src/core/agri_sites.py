"""智慧农业专题监测数据源（7 个央企招采网站，非农业专用平台）。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import yaml

from src.core.site_sync import get_site_by_id, load_sites_config

ROOT = Path(__file__).resolve().parent.parent.parent
AGRI_SITES_PATH = ROOT / "config" / "agri_sites.yaml"

DEFAULT_AGRI_SITE_IDS: tuple[str, ...] = (
    "中国建筑集团有限公司_云筑网",
    "crec_bidding",
    "dlzb_power",
    "中国铁道建筑集团有限公司_物资采购网",
    "中国交通建设集团有限公司_供应链管理信息系统",
    "中国电力建设集团有限公司_公共资源交易服务平台",
    "中国能源建设集团有限公司_电子采购平台",
)


@lru_cache(maxsize=1)
def load_agri_sites_config() -> dict[str, Any]:
    if not AGRI_SITES_PATH.is_file():
        return {
            "site_ids": list(DEFAULT_AGRI_SITE_IDS),
            "sites": [],
            "search_keywords": [],
            "scope_label": "七大央企招采数据源",
            "default_max_items": 30,
            "max_search_keywords_per_site": 0,
        }
    with open(AGRI_SITES_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_agri_site_ids() -> list[str]:
    cfg = load_agri_sites_config()
    ids = cfg.get("site_ids") or list(DEFAULT_AGRI_SITE_IDS)
    return [str(i) for i in ids if str(i).strip()]


def get_agri_scope_label() -> str:
    cfg = load_agri_sites_config()
    return str(cfg.get("scope_label") or "七大央企招采数据源")


def get_agri_search_keywords() -> list[str]:
    cfg = load_agri_sites_config()
    kws = cfg.get("search_keywords") or []
    return [str(k).strip() for k in kws if str(k).strip()]


def get_agri_site_meta(site_id: str) -> dict[str, Any]:
    cfg = load_agri_sites_config()
    for item in cfg.get("sites") or []:
        if item.get("id") == site_id:
            return dict(item)
    return {"id": site_id, "name": site_id}


def resolve_agri_search_url(site_id: str, keyword: str) -> Optional[str]:
    meta = get_agri_site_meta(site_id)
    template = meta.get("search_url_template")
    if not template or not keyword.strip():
        return None
    return template.format(keyword=quote(keyword.strip()))


def load_agri_sites(*, site_id: Optional[str] = None) -> list[dict[str, Any]]:
    """返回 agri 专用站点配置（不受 crawl_scope 过滤，不要求 industry）。"""
    config = load_sites_config()
    crawl_defaults = config.get("crawl_defaults", {})
    allowed = set(get_agri_site_ids())
    if site_id:
        canonical = site_id.strip()
        allowed = {canonical} if canonical in allowed else set()

    sites: list[dict[str, Any]] = []
    for raw in config.get("sites", []):
        sid = raw.get("id")
        if sid not in allowed:
            continue
        if not raw.get("enabled"):
            continue
        meta = get_agri_site_meta(sid)
        merged = {**crawl_defaults, **raw}
        merged["agri_display_name"] = meta.get("name") or raw.get("name") or sid
        sites.append(merged)
    return sites


def apply_agri_entry_override(
    site: dict[str, Any],
    *,
    keyword: Optional[str] = None,
) -> dict[str, Any]:
    """为带搜索模板的站点注入 entry_url 覆盖（sync 前调用）。"""
    sid = site.get("id", "")
    if not keyword:
        return site
    url = resolve_agri_search_url(sid, keyword)
    if not url:
        return site
    patched = dict(site)
    patched["agri_entry_url_override"] = url
    patched["agri_search_keyword"] = keyword
    return patched


def agri_site_display_list() -> list[dict[str, Any]]:
    """8091 UI 监测站点列表。"""
    result: list[dict[str, Any]] = []
    for site in load_agri_sites():
        sid = site["id"]
        meta = get_agri_site_meta(sid)
        result.append(
            {
                "id": sid,
                "name": meta.get("name") or site.get("name") or sid,
                "enabled": True,
                "parent": meta.get("parent") or site.get("parent"),
                "notes": meta.get("notes") or site.get("notes") or "",
            }
        )
    return result
