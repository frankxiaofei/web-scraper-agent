"""公告通用标签：入库赋值与检索辅助。"""

from __future__ import annotations

from typing import Any, Optional

from src.core.models import BidNotice

WECHAT_TAG = "微信公众号"


def is_wechat_doc(doc: dict[str, Any]) -> bool:
    """判断 Mongo/JSONL 文档是否为微信公众号来源。"""
    if doc.get("wechat"):
        return True
    for key in ("source_site_id", "source", "site_id"):
        if str(doc.get(key) or "").startswith("wechat_"):
            return True
    return str(doc.get("category") or "").strip().lower() == "wechat"


def is_wechat_site(site: dict[str, Any]) -> bool:
    site_id = str(site.get("id") or "")
    if site_id.startswith("wechat_"):
        return True
    return str(site.get("category") or "").strip().lower() == "wechat"


def is_wechat_notice(notice: BidNotice, site: Optional[dict[str, Any]] = None) -> bool:
    if str(notice.source_site_id or "").startswith("wechat_"):
        return True
    if site:
        return is_wechat_site(site)
    return False


def effective_notice_tags(doc: dict[str, Any]) -> list[str]:
    """文档有效标签（含历史微信数据回退）。"""
    tags: list[str] = []
    seen: set[str] = set()
    for raw in doc.get("tags") or []:
        tag = str(raw).strip()
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    if WECHAT_TAG not in seen and is_wechat_doc(doc):
        tags.append(WECHAT_TAG)
    return tags


def apply_notice_tags(notice: BidNotice, site: Optional[dict[str, Any]] = None) -> BidNotice:
    """入库前确保来源类标签已写入 notice.tags。"""
    tags = list(notice.tags or [])
    if is_wechat_notice(notice, site) and WECHAT_TAG not in tags:
        tags.append(WECHAT_TAG)
    notice.tags = tags or None
    return notice


def normalize_user_tags(tags: list[str] | None) -> list[str]:
    """用户输入标签去重、去空白。"""
    result: list[str] = []
    seen: set[str] = set()
    for raw in tags or []:
        tag = str(raw).strip()
        if not tag or len(tag) > 64 or tag in seen:
            continue
        seen.add(tag)
        result.append(tag)
    return result


def persistable_notice_tags(doc: dict[str, Any], user_tags: list[str] | None) -> list[str]:
    """用户编辑后的可持久化 tags（微信来源保留「微信公众号」）。"""
    tags = normalize_user_tags(user_tags)
    if is_wechat_doc(doc) and WECHAT_TAG not in tags:
        tags.append(WECHAT_TAG)
    return tags
