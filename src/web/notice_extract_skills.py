"""公告结构化字段抽取 skill — 合同金额、招标方、项目周期、招标关键内容。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from src.core.config import get_settings
from src.core.notice_field_utils import extract_notice_fields_from_doc
from src.db.mongo import create_mongo_repository
from src.db.mongo_repository import MongoRepository
from src.web.data_service import get_data_service

logger = logging.getLogger(__name__)

_mongo_repo: Optional[MongoRepository] = None

_PERSIST_FIELDS = (
    "contract_amount",
    "budget_amount",
    "tender_party",
    "purchaser",
    "project_period",
    "key_summary",
)


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


def _powerchina_detail_url(notice_id: str) -> str:
    return f"https://bid.powerchina.cn/notice/detail?id={notice_id.strip()}"


def _resolve_notice_doc(
    *,
    doc: Optional[dict[str, Any]] = None,
    site_id: Optional[str] = None,
    notice_id: Optional[str] = None,
    url: Optional[str] = None,
    content_text: Optional[str] = None,
    title: Optional[str] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """解析输入为公告文档；返回 (doc, error)。"""
    if doc:
        return dict(doc), None

    if url or notice_id:
        svc = get_data_service()
        lookup = (url or "").strip()
        if not lookup and notice_id:
            lookup = _powerchina_detail_url(notice_id)
        found = svc.get_notice(lookup)
        if found:
            return found, None
        if notice_id and not url:
            found = svc.get_notice(notice_id.strip())
            if found:
                return found, None

    text = (content_text or "").strip()
    if text or title:
        return {
            "title": (title or "").strip(),
            "content_text": text,
            "url": (url or "").strip() or None,
            "source_site_id": (site_id or "").strip() or None,
            "site_id": (site_id or "").strip() or None,
        }, None

    return None, "请提供 doc、site_id+notice_id、url+content_text 或 url/notice_id 定位已有公告"


def _persist_fields(doc: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    repo = _get_mongo_repo()
    if repo is None:
        return {"ok": False, "persisted": False, "error": "MongoDB 不可用（检查 MONGODB_URI）"}

    content_hash = doc.get("content_hash")
    url = doc.get("url") or doc.get("detail_url")
    if not content_hash and not url:
        return {"ok": False, "persisted": False, "error": "文档缺少 content_hash/url，无法持久化"}

    update_doc: dict[str, Any] = {"fields_extracted_at": datetime.now(timezone.utc)}
    if fields.get("contract_amount"):
        update_doc["contract_amount"] = fields["contract_amount"]
    if fields.get("budget_amount"):
        update_doc["budget_amount"] = fields["budget_amount"]
    if fields.get("tender_party"):
        update_doc["tender_party"] = fields["tender_party"]
        if not doc.get("purchaser"):
            update_doc["purchaser"] = fields["tender_party"]
    if fields.get("project_period"):
        update_doc["project_period"] = fields["project_period"]
    if fields.get("key_content"):
        update_doc["key_summary"] = fields["key_content"]

    filt: dict[str, Any]
    if content_hash:
        filt = {"content_hash": content_hash}
    else:
        filt = {"$or": [{"url": url}, {"detail_url": url}]}

    try:
        result = repo._notices.update_one(filt, {"$set": update_doc})  # noqa: SLF001
        if doc.get("is_bim_related") and repo._bim_notices is not None:
            repo._bim_notices.update_one(filt, {"$set": update_doc})  # noqa: SLF001
        persisted = bool(result.matched_count and result.modified_count)
        return {
            "ok": True,
            "persisted": persisted,
            "updated_fields": [k for k in _PERSIST_FIELDS if k in update_doc],
        }
    except Exception as exc:
        logger.warning("持久化抽取字段失败 %s: %s", url or content_hash, exc)
        return {"ok": False, "persisted": False, "error": str(exc)}


def extract_notice_fields(
    *,
    doc: Optional[dict[str, Any]] = None,
    site_id: Optional[str] = None,
    notice_id: Optional[str] = None,
    url: Optional[str] = None,
    content_text: Optional[str] = None,
    title: Optional[str] = None,
    persist: bool = False,
    use_llm: Optional[bool] = None,
) -> dict[str, Any]:
    """抽取公告结构化字段（LLM 优先，规则回退；有正文时默认启用 LLM）。"""
    resolved, err = _resolve_notice_doc(
        doc=doc,
        site_id=site_id,
        notice_id=notice_id,
        url=url,
        content_text=content_text,
        title=title,
    )
    if err or not resolved:
        return {"ok": False, "error": err or "无法解析输入"}

    fields = extract_notice_fields_from_doc(resolved, use_llm=use_llm)
    output = {
        "ok": True,
        "site_id": resolved.get("source_site_id") or resolved.get("site_id") or site_id,
        "url": resolved.get("url") or url,
        "title": resolved.get("title") or title,
        "contract_amount": fields.get("contract_amount"),
        "tender_party": fields.get("tender_party"),
        "project_period": fields.get("project_period"),
        "key_content": fields.get("key_content"),
        "amount_yuan": fields.get("amount_yuan"),
        "extraction_source": fields.get("extraction_source"),
    }

    if persist:
        persist_result = _persist_fields(resolved, fields)
        output["persisted"] = persist_result.get("persisted", False)
        if persist_result.get("error"):
            output["persist_error"] = persist_result["error"]
        if persist_result.get("updated_fields"):
            output["updated_fields"] = persist_result["updated_fields"]

    return output
