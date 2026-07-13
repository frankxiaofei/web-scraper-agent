"""带标签文档写入 MongoDB tagged_documents 集合。"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

from src.core.config import get_settings
from src.db.mongo import create_mongo_repository
from src.db.mongo_repository import MongoRepository

logger = logging.getLogger(__name__)

_mongo_repo: Optional[MongoRepository] = None


def _get_mongo_repo() -> Optional[MongoRepository]:
    global _mongo_repo
    if _mongo_repo is None:
        settings = get_settings()
        _mongo_repo = create_mongo_repository(
            uri=settings.mongodb_uri,
            db_name=settings.mongodb_db,
            collection_name=settings.mongodb_collection,
        )
    if _mongo_repo is None or not _mongo_repo.available:
        return None
    return _mongo_repo


def _normalize_tags(tags: Any) -> list[str]:
    if not tags:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = str(raw).strip()
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


def _content_hash(*, title: str, url: Optional[str], content: Optional[str]) -> str:
    raw = f"{url or ''}|{title}|{(content or '')[:500]}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def save_tagged_document(
    *,
    title: str,
    tags: list[str],
    url: Optional[str] = None,
    content: Optional[str] = None,
    site_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    source: Optional[str] = None,
) -> dict[str, Any]:
    """写入 tagged_documents（url 或 content_hash 去重）。"""
    title = (title or "").strip()
    url = (url or "").strip() or None
    content = (content or "").strip() or None
    site_id = (site_id or "").strip() or None
    source = (source or "").strip() or None
    normalized_tags = _normalize_tags(tags)

    if not title and not content:
        return {"ok": False, "error": "title 或 content 至少填一项"}
    if not normalized_tags:
        return {"ok": False, "error": "tags 不能为空"}

    repo = _get_mongo_repo()
    if repo is None:
        return {"ok": False, "saved": False, "error": "MongoDB 不可用（检查 MONGODB_URI）"}

    content_hash = _content_hash(title=title or url or "untitled", url=url, content=content)
    result = repo.upsert_tagged_document(
        title=title or url or "untitled",
        tags=normalized_tags,
        url=url,
        content=content,
        site_id=site_id,
        metadata=metadata if isinstance(metadata, dict) else None,
        source=source,
        content_hash=content_hash,
    )
    if result.get("ok"):
        result.setdefault("title", title)
        result.setdefault("url", url)
        result.setdefault("tags", normalized_tags)
        result.setdefault("site_id", site_id)
    return result


def search_tagged_documents(
    tags: list[str],
    *,
    match_mode: str = "any",
    site_id: Optional[str] = None,
    limit: int = 20,
) -> dict[str, Any]:
    """按标签检索 tagged_documents。"""
    normalized_tags = _normalize_tags(tags)
    if not normalized_tags:
        return {"ok": False, "error": "tags 不能为空"}

    mode = (match_mode or "any").strip().lower()
    if mode not in ("any", "all"):
        return {"ok": False, "error": "match_mode 须为 any 或 all"}

    repo = _get_mongo_repo()
    if repo is None:
        return {"ok": False, "error": "MongoDB 不可用（检查 MONGODB_URI）"}

    site_id = (site_id or "").strip() or None
    safe_limit = max(1, min(int(limit or 20), 200))
    items = repo.find_tagged_by_tags(
        normalized_tags,
        match_mode=mode,
        site_id=site_id,
        limit=safe_limit,
    )
    return {
        "ok": True,
        "tags": normalized_tags,
        "match_mode": mode,
        "site_id": site_id,
        "total": len(items),
        "items": items,
    }
