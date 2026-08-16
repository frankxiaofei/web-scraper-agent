"""招标公告数据读取：MongoDB 优先，JSONL 降级。

列表与 API 默认仅返回 config/sites.yaml 中 enabled: true 的 MVP 站点公告。
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.core.config import get_settings
from src.core.timezone_utils import APP_TZ, coerce_dt, format_display_dt, to_app_tz
from src.core.notice_tags import (
    WECHAT_TAG,
    effective_notice_tags,
    is_wechat_doc,
    persistable_notice_tags,
)
from src.core.site_sync import SITES_PATH, load_enabled_sites
from src.core.url_utils import is_browsable_http_url, resolve_notice_detail_url
from src.web.notice_html import prepare_notice_html_for_display

logger = logging.getLogger(__name__)

SUMMARY_MAX_LEN = 160
_TAGS_CACHE_TTL_SECONDS = 60.0
_LIST_MONGO_PROJECTION = {"content_html": 0, "content_text": 0, "attachments": 0}


def notice_key_info_dict_from_doc(doc: dict[str, Any]) -> dict[str, str]:
    """从 Mongo/JSONL 文档组装 key_info（API 用）。"""
    def _fmt(key: str, *fallback_keys: str) -> str:
        val = doc.get(key)
        if val:
            return str(val)
        for fk in fallback_keys:
            fv = doc.get(fk)
            if fv:
                return str(fv)
        return "—"

    return {
        "contract_amount": _fmt("contract_amount"),
        "budget_amount": _fmt("budget_amount", "budget"),
        "tender_party": _fmt("tender_party", "purchaser"),
        "agency": _fmt("agency"),
        "project_period": _fmt("project_period"),
        "key_summary": _fmt("key_summary"),
        "bid_deadline": _fmt("bid_deadline"),
        "open_time": _fmt("open_time"),
        "contact": _fmt("contact"),
    }


def _parse_dt(value: Any) -> Optional[datetime]:
    return coerce_dt(value)


def _format_dt(value: Any) -> str:
    return format_display_dt(value)


_PUBLISH_TIME_KEYS = ("publish_date", "published_at")
_CRAWL_TIME_KEYS = ("scraped_at", "crawled_at", "created_at")
NOTICE_MONGO_SORT = [
    ("publish_date", -1),
    ("published_at", -1),
    ("scraped_at", -1),
    ("crawled_at", -1),
    ("_id", -1),
]


def doc_crawl_timestamp(doc: dict[str, Any]) -> Optional[datetime]:
    """采集/入库时间。"""
    for key in _CRAWL_TIME_KEYS:
        dt = _parse_dt(doc.get(key))
        if dt is not None:
            return dt
    return None


def doc_source_id(doc: dict[str, Any]) -> str:
    """公告来源站点 ID（兼容 source / site_id 字段）。"""
    return doc.get("source_site_id") or doc.get("source") or doc.get("site_id") or ""


def doc_publish_timestamp(
    doc: dict[str, Any],
    *,
    warn_fallback: bool = False,
) -> Optional[datetime]:
    """公告发布时间；缺失时回退采集时间。"""
    for key in _PUBLISH_TIME_KEYS:
        dt = _parse_dt(doc.get(key))
        if dt is not None:
            return dt
    crawl_ts = doc_crawl_timestamp(doc)
    if crawl_ts is not None:
        if warn_fallback:
            logger.warning(
                "公告缺少发布时间，回退采集时间 site=%s title=%s",
                doc.get("source_site_id") or doc.get("source") or "",
                (doc.get("title") or "")[:60],
            )
        return crawl_ts
    return None


def _make_summary(doc: dict[str, Any]) -> str:
    ki = doc.get("key_summary")
    if ki:
        text = re.sub(r"\s+", " ", str(ki)).strip()
        if len(text) <= SUMMARY_MAX_LEN:
            return text
        return text[:SUMMARY_MAX_LEN].rstrip() + "…"
    for key in ("summary", "content_text", "title"):
        raw = doc.get(key)
        if not raw:
            continue
        text = re.sub(r"\s+", " ", str(raw)).strip()
        if len(text) <= SUMMARY_MAX_LEN:
            return text
        return text[:SUMMARY_MAX_LEN].rstrip() + "…"
    return "—"


def notice_id_from_doc(doc: dict[str, Any]) -> str:
    """与列表/详情路由一致的公告 ID（Mongo _id 或 content_hash）。"""
    notice_id = doc.get("_id")
    if notice_id is not None:
        return str(notice_id)
    detail_url = doc.get("detail_url") or doc.get("url") or ""
    return str(doc.get("content_hash") or detail_url or doc.get("id") or "")


def notice_content_href(notice_id: str) -> str:
    """爬取正文详情页 URL（详情页正文区块锚点）。"""
    if not notice_id:
        return ""
    return f"/notices/{notice_id}#notice-content"


def _normalize_doc(
    doc: dict[str, Any],
    *,
    for_list: bool = False,
    assume_has_content: bool = False,
) -> dict[str, Any]:
    """统一 Mongo / JSONL 字段名，供模板与 API 使用。"""
    source_id = doc.get("source_site_id") or doc.get("source") or doc.get("site_id") or ""
    source_base = doc.get("source_url") or ""
    raw_detail_url = doc.get("detail_url") or doc.get("url") or ""
    detail_url = resolve_notice_detail_url(raw_detail_url, base_url=source_base)
    source_name = doc.get("source_site_name") or source_id or "—"
    scraped = doc.get("scraped_at") or doc.get("crawled_at")
    publish = doc.get("publish_date")

    notice_id = notice_id_from_doc(doc)

    raw_has_content = bool(
        (doc.get("content_text") or "").strip() or (doc.get("content_html") or "").strip()
    )

    if for_list:
        budget_display = doc.get("budget_amount") or doc.get("budget") or "—"
        tender_display = doc.get("tender_party") or doc.get("purchaser") or "—"
        return {
            "id": notice_id,
            "title": doc.get("title") or "（无标题）",
            "source_site_id": source_id,
            "source_site_name": source_name,
            "publish_date": publish,
            "publish_date_fmt": _format_dt(publish),
            "region": doc.get("region") or "—",
            "summary": _make_summary(doc),
            "detail_url": detail_url if is_browsable_http_url(detail_url) else "",
            "budget_amount": budget_display if budget_display != "—" else "—",
            "tender_party": tender_display if tender_display != "—" else "—",
            "has_content": assume_has_content or raw_has_content,
            "tags": effective_notice_tags(doc),
        }

    attachments = doc.get("attachments") or []
    if isinstance(attachments, str):
        try:
            attachments = json.loads(attachments.replace("'", '"'))
        except (json.JSONDecodeError, TypeError):
            attachments = []

    budget_display = doc.get("budget_amount") or doc.get("budget") or "—"
    tender_display = doc.get("tender_party") or doc.get("purchaser") or "—"

    key_info = notice_key_info_dict_from_doc(doc)

    is_wechat = is_wechat_doc(doc)
    display_attachments = attachments
    if is_wechat:
        display_attachments = [
            att
            for att in attachments
            if att.get("type") != "image"
        ]

    return {
        "id": notice_id,
        "title": doc.get("title") or "（无标题）",
        "source_site_id": source_id,
        "source_site_name": source_name,
        "publish_date": publish,
        "publish_date_fmt": _format_dt(publish),
        "region": doc.get("region") or "—",
        "depth": doc.get("depth", 0),
        "summary": _make_summary(doc),
        "detail_url": detail_url if is_browsable_http_url(detail_url) else "",
        "purchaser": doc.get("purchaser") or "—",
        "agency": doc.get("agency") or "—",
        "budget": doc.get("budget") or "—",
        "budget_amount": budget_display if budget_display != "—" else "—",
        "tender_party": tender_display if tender_display != "—" else "—",
        "contract_amount": doc.get("contract_amount") or "—",
        "project_period": doc.get("project_period") or "—",
        "key_summary": doc.get("key_summary") or "—",
        "bid_deadline": doc.get("bid_deadline") or "—",
        "open_time": doc.get("open_time") or "—",
        "contact": doc.get("contact") or "—",
        "key_info": key_info,
        "content_text": doc.get("content_text") or "",
        "content_html": prepare_notice_html_for_display(
            doc.get("content_html"),
            attachments=attachments if isinstance(attachments, list) else [],
            is_wechat=is_wechat,
        ),
        "attachments": display_attachments,
        "is_wechat": is_wechat,
        "scraped_at": scraped,
        "scraped_at_fmt": _format_dt(scraped),
        "parent_url": doc.get("parent_url") or "",
        "page_type": doc.get("page_type") or "—",
        "category": doc.get("category") or "—",
        "content_hash": doc.get("content_hash") or "",
        "has_content": raw_has_content,
        "is_agri_related": doc.get("is_agri_related"),
        "agri_confidence": doc.get("agri_confidence"),
        "agri_reason": doc.get("agri_reason") or "",
        "agri_tags": doc.get("agri_tags") or [],
        "tags": effective_notice_tags(doc),
    }


class NoticeDataService:
    """公告列表/详情查询。"""

    def __init__(self) -> None:
        settings = get_settings()
        self._jsonl_path = settings.data_dir / "notices.jsonl"
        self._mongo_coll = None
        self._mongo_available = False
        self._jsonl_cache: Optional[list[dict[str, Any]]] = None
        self._jsonl_mtime: float = 0.0
        self._enabled_site_ids_cache: Optional[list[str]] = None
        self._enabled_site_ids_mtime: float = 0.0
        self._tags_cache: Optional[tuple[float, list[str]]] = None
        self._connect_mongo(settings)

    def _enabled_site_ids(self) -> list[str]:
        """MVP 站点 ID 列表（sites.yaml enabled: true）。"""
        mtime = SITES_PATH.stat().st_mtime if SITES_PATH.exists() else 0.0
        if self._enabled_site_ids_cache is None or mtime != self._enabled_site_ids_mtime:
            self._enabled_site_ids_cache = [s["id"] for s in load_enabled_sites()]
            self._enabled_site_ids_mtime = mtime
        return self._enabled_site_ids_cache

    @staticmethod
    def _doc_source_id(doc: dict[str, Any]) -> str:
        return doc.get("source_site_id") or doc.get("source") or doc.get("site_id") or ""

    def _in_mvp_scope(self, doc: dict[str, Any]) -> bool:
        sid = self._doc_source_id(doc)
        return sid in self._enabled_site_ids()

    @staticmethod
    def _mvp_source_filter_mongo(mvp_ids: list[str] | None) -> dict[str, Any]:
        ids = list(mvp_ids or [])
        if not ids:
            return {"_id": {"$exists": False}}
        return {
            "$or": [
                {"source_site_id": {"$in": ids}},
                {"source": {"$in": ids}},
                {"site_id": {"$in": ids}},
            ]
        }

    @staticmethod
    def _tag_filter_mongo(tag: str) -> dict[str, Any]:
        if tag == WECHAT_TAG:
            return {
                "$or": [
                    {"tags": tag},
                    {"wechat": True},
                    {"source_site_id": {"$regex": r"^wechat_"}},
                    {"source": {"$regex": r"^wechat_"}},
                    {"site_id": {"$regex": r"^wechat_"}},
                    {"category": "wechat"},
                ]
            }
        return {"tags": tag}

    @classmethod
    def _tags_filter_mongo(cls, tags: list[str]) -> dict[str, Any]:
        cleaned = [t.strip() for t in tags if t and t.strip()]
        if not cleaned:
            return {}
        if len(cleaned) == 1:
            return cls._tag_filter_mongo(cleaned[0])
        return {"$or": [cls._tag_filter_mongo(tag) for tag in cleaned]}

    @staticmethod
    def _site_ids_filter_mongo(site_ids: list[str]) -> dict[str, Any]:
        return {
            "$or": [
                {"source_site_id": {"$in": site_ids}},
                {"source": {"$in": site_ids}},
                {"site_id": {"$in": site_ids}},
            ]
        }

    def _connect_mongo(self, settings) -> None:
        from src.db.mongo_client import get_mongo_database

        db = get_mongo_database(settings.mongodb_uri, settings.mongodb_db)
        if db is None:
            self._mongo_coll = None
            return
        self._mongo_coll = db[settings.mongodb_collection]
        self._mongo_available = True
        if self._mongo_has_mvp_data():
            logger.info("Web UI 使用 MongoDB 数据源")
        else:
            logger.info("MongoDB 已连接但无 MVP 公告，Web UI 回退 JSONL")

    def _mongo_has_mvp_data(self) -> bool:
        """Mongo 中是否存在 enabled 站点公告；空库时不应掩盖 JSONL 数据。"""
        if not self._mongo_available or self._mongo_coll is None:
            return False
        mvp_ids = self._enabled_site_ids()
        if not mvp_ids:
            return False
        try:
            return (
                self._mongo_coll.count_documents(
                    self._mvp_source_filter_mongo(mvp_ids),
                    limit=1,
                )
                > 0
            )
        except Exception:
            logger.debug("MongoDB MVP 计数失败，回退 JSONL", exc_info=True)
            return False

    def _prefer_mongo_for_list(self) -> bool:
        return self._mongo_available and self._mongo_coll is not None and self._mongo_has_mvp_data()

    @property
    def data_source(self) -> str:
        return "mongodb" if self._prefer_mongo_for_list() else "jsonl"

    def _load_jsonl(self) -> list[dict[str, Any]]:
        mtime = self._jsonl_path.stat().st_mtime if self._jsonl_path.is_file() else 0.0
        if self._jsonl_cache is not None and mtime == self._jsonl_mtime:
            return self._jsonl_cache
        docs: list[dict[str, Any]] = []
        if not self._jsonl_path.is_file():
            self._jsonl_cache = docs
            self._jsonl_mtime = mtime
            return docs
        with self._jsonl_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    docs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        def _jsonl_sort_key(doc: dict[str, Any]) -> datetime:
            ts = doc_publish_timestamp(doc)
            if ts is None:
                return datetime.min.replace(tzinfo=APP_TZ)
            return to_app_tz(ts)

        docs.sort(key=_jsonl_sort_key, reverse=True)
        self._jsonl_cache = docs
        self._jsonl_mtime = mtime
        return docs

    def _match_filters(
        self,
        doc: dict[str, Any],
        *,
        site_ids: Optional[list[str]] = None,
        keyword: Optional[str] = None,
        has_content: bool = True,
        bim_only: bool = False,
        agri_only: bool = False,
        tags: Optional[list[str]] = None,
    ) -> bool:
        if not self._in_mvp_scope(doc):
            return False
        doc_tags = effective_notice_tags(doc)
        if tags:
            wanted = [t.strip() for t in tags if t and t.strip()]
            if wanted and not any(tag in doc_tags for tag in wanted):
                return False
        only_agri = agri_only or bim_only
        if only_agri:
            from src.core.agri_classifier import doc_is_agri_related

            if not doc_is_agri_related(doc):
                return False
        if site_ids:
            sid = self._doc_source_id(doc)
            if sid not in site_ids:
                return False
        if has_content and not only_agri:
            text = (doc.get("content_text") or "").strip()
            html = (doc.get("content_html") or "").strip()
            page_type = (doc.get("page_type") or "").lower()
            if not text and not html:
                return False
            if page_type in ("list", "index"):
                return False
        if keyword:
            kw = keyword.strip().lower()
            if not kw:
                return True
            haystack = " ".join(
                str(doc.get(k) or "")
                for k in ("title", "content_text", "region", "purchaser", "source_site_name")
            ).lower()
            haystack = f"{haystack} {' '.join(doc_tags).lower()}"
            if kw not in haystack:
                return False
        return True

    @staticmethod
    def _resolve_list_filters(
        *,
        source: Optional[str] = None,
        site_ids: Optional[list[str]] = None,
        tag: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> tuple[list[str], list[str]]:
        resolved_site_ids: list[str] = []
        seen_sites: set[str] = set()
        for value in list(site_ids or []) + ([source] if source else []):
            sid = (value or "").strip()
            if sid and sid not in seen_sites:
                seen_sites.add(sid)
                resolved_site_ids.append(sid)

        resolved_tags: list[str] = []
        seen_tags: set[str] = set()
        for value in list(tags or []) + ([tag] if tag else []):
            tag_value = (value or "").strip()
            if tag_value and tag_value not in seen_tags:
                seen_tags.add(tag_value)
                resolved_tags.append(tag_value)
        return resolved_site_ids, resolved_tags

    def list_sources(self) -> list[dict[str, str]]:
        """来源下拉：仅 MVP enabled 站点（来自 sites.yaml，非全库聚合）。"""
        sites = load_enabled_sites()
        return [
            {"id": s["id"], "name": s.get("name") or s["id"]}
            for s in sites
        ]

    def list_tags(self) -> list[str]:
        """公告列表可用标签（含固定标签与库内去重）。"""
        now = time.monotonic()
        if self._tags_cache is not None:
            cached_at, cached_tags = self._tags_cache
            if now - cached_at < _TAGS_CACHE_TTL_SECONDS:
                return cached_tags

        tags: set[str] = {WECHAT_TAG}
        if self._prefer_mongo_for_list():
            try:
                for row in self._mongo_coll.aggregate(
                    [
                        {"$match": self._mvp_source_filter_mongo(self._enabled_site_ids())},
                        {"$unwind": "$tags"},
                        {"$group": {"_id": "$tags"}},
                    ]
                ):
                    value = str(row.get("_id") or "").strip()
                    if value:
                        tags.add(value)
            except Exception:
                logger.debug("MongoDB 标签聚合失败，使用默认标签集", exc_info=True)
        else:
            for doc in self._load_jsonl():
                if not self._in_mvp_scope(doc):
                    continue
                tags.update(effective_notice_tags(doc))
        result = sorted(tags)
        self._tags_cache = (now, result)
        return result

    def list_notices(
        self,
        *,
        page: int = 1,
        per_page: int = 20,
        source: Optional[str] = None,
        site_ids: Optional[list[str]] = None,
        keyword: Optional[str] = None,
        has_content: bool = True,
        agri_only: bool = False,
        bim_only: bool = False,
        tag: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        page = max(1, page)
        per_page = max(1, min(per_page, 100))
        skip = (page - 1) * per_page
        resolved_site_ids, resolved_tags = self._resolve_list_filters(
            source=source,
            site_ids=site_ids,
            tag=tag,
            tags=tags,
        )

        if self._prefer_mongo_for_list():
            return self._list_from_mongo(
                page=page,
                per_page=per_page,
                skip=skip,
                site_ids=resolved_site_ids or None,
                keyword=keyword,
                has_content=has_content,
                agri_only=agri_only,
                bim_only=bim_only,
                tags=resolved_tags or None,
            )
        return self._list_from_jsonl(
            page=page,
            per_page=per_page,
            skip=skip,
            site_ids=resolved_site_ids or None,
            keyword=keyword,
            has_content=has_content,
            agri_only=agri_only,
            bim_only=bim_only,
            tags=resolved_tags or None,
        )

    def _list_from_mongo(
        self,
        *,
        page: int,
        per_page: int,
        skip: int,
        site_ids: Optional[list[str]],
        keyword: Optional[str],
        has_content: bool = True,
        agri_only: bool = False,
        bim_only: bool = False,
        tags: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        coll = self._mongo_coll
        mvp_ids = self._enabled_site_ids()
        if not mvp_ids:
            return self._page_result([], total=0, page=page, per_page=per_page)
        if site_ids:
            valid_site_ids = [sid for sid in site_ids if sid in mvp_ids]
            if not valid_site_ids:
                return self._page_result([], total=0, page=page, per_page=per_page)

        query: dict[str, Any] = {}
        query["$and"] = [self._mvp_source_filter_mongo(mvp_ids)]
        if tags:
            tag_clause = self._tags_filter_mongo(tags)
            if tag_clause:
                query["$and"].append(tag_clause)
        if site_ids:
            query["$and"].append(self._site_ids_filter_mongo(valid_site_ids))
        only_agri = agri_only or bim_only
        if only_agri:
            query["$and"].append(
                {
                    "$or": [
                        {"is_agri_related": True},
                        {"agri_tags": {"$exists": True, "$ne": []}},
                    ]
                }
            )
        if has_content and not only_agri:
            query["$and"] = query.get("$and", []) + [
                {
                    "$or": [
                        {"content_text": {"$exists": True, "$nin": [None, ""]}},
                        {"content_html": {"$exists": True, "$nin": [None, ""]}},
                    ]
                },
                {"page_type": {"$nin": ["list", "index"]}},
            ]
        if keyword and keyword.strip():
            kw = re.escape(keyword.strip())
            query["$and"] = query.get("$and", []) + [
                {
                    "$or": [
                        {"title": {"$regex": kw, "$options": "i"}},
                        {"content_text": {"$regex": kw, "$options": "i"}},
                        {"region": {"$regex": kw, "$options": "i"}},
                        {"purchaser": {"$regex": kw, "$options": "i"}},
                        {"source_site_name": {"$regex": kw, "$options": "i"}},
                        {"tags": {"$regex": kw, "$options": "i"}},
                    ]
                }
            ]

        total = coll.count_documents(query)
        cursor = (
            coll.find(query, _LIST_MONGO_PROJECTION)
            .sort(NOTICE_MONGO_SORT)
            .skip(skip)
            .limit(per_page)
        )
        items = [
            _normalize_doc(doc, for_list=True, assume_has_content=has_content and not only_agri)
            for doc in cursor
        ]
        return self._page_result(items, total=total, page=page, per_page=per_page)

    def _list_from_jsonl(
        self,
        *,
        page: int,
        per_page: int,
        skip: int,
        site_ids: Optional[list[str]],
        keyword: Optional[str],
        has_content: bool = True,
        agri_only: bool = False,
        bim_only: bool = False,
        tags: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        filtered = [
            doc
            for doc in self._load_jsonl()
            if self._match_filters(
                doc,
                site_ids=site_ids,
                keyword=keyword,
                has_content=has_content,
                agri_only=agri_only,
                bim_only=bim_only,
                tags=tags,
            )
        ]
        total = len(filtered)
        page_docs = filtered[skip : skip + per_page]
        only_agri = agri_only or bim_only
        items = [
            _normalize_doc(doc, for_list=True, assume_has_content=has_content and not only_agri)
            for doc in page_docs
        ]
        return self._page_result(items, total=total, page=page, per_page=per_page)

    @staticmethod
    def _page_result(
        items: list[dict[str, Any]],
        *,
        total: int,
        page: int,
        per_page: int,
    ) -> dict[str, Any]:
        pages = max(1, (total + per_page - 1) // per_page)
        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
            "has_prev": page > 1,
            "has_next": page < pages,
        }

    def get_notice(self, notice_id: str) -> Optional[dict[str, Any]]:
        if self._mongo_available and self._mongo_coll is not None:
            doc = self._get_from_mongo(notice_id)
            if doc:
                return _normalize_doc(doc)

        for doc in self._load_jsonl():
            cid = doc.get("content_hash") or doc.get("url") or ""
            if cid == notice_id or doc.get("url") == notice_id:
                return _normalize_doc(doc)
        return None

    def _get_from_mongo(self, notice_id: str) -> Optional[dict[str, Any]]:
        coll = self._mongo_coll
        from bson import ObjectId
        from bson.errors import InvalidId

        try:
            oid = ObjectId(notice_id)
            doc = coll.find_one({"_id": oid})
            if doc:
                return doc
        except InvalidId:
            pass

        for field in ("content_hash", "url", "detail_url"):
            doc = coll.find_one({field: notice_id})
            if doc:
                return doc
        return None

    @staticmethod
    def _doc_matches_id(doc: dict[str, Any], notice_id: str) -> bool:
        if doc.get("_id") is not None and str(doc["_id"]) == notice_id:
            return True
        cid = doc.get("content_hash") or doc.get("url") or ""
        return cid == notice_id or doc.get("url") == notice_id

    def _find_raw_doc(self, notice_id: str) -> Optional[dict[str, Any]]:
        notice_id = (notice_id or "").strip()
        if not notice_id:
            return None
        if self._mongo_available and self._mongo_coll is not None:
            doc = self._get_from_mongo(notice_id)
            if doc:
                return doc
        for doc in self._load_jsonl():
            if self._doc_matches_id(doc, notice_id):
                return doc
        return None

    @staticmethod
    def _mongo_filter_for_doc(doc: dict[str, Any]) -> dict[str, Any]:
        notice_id = doc.get("_id")
        if notice_id is not None:
            return {"_id": notice_id}
        content_hash = doc.get("content_hash")
        if content_hash:
            return {"content_hash": content_hash}
        url = doc.get("url") or doc.get("detail_url")
        if url:
            return {"$or": [{"url": url}, {"detail_url": url}]}
        raise ValueError("文档缺少 _id/content_hash/url，无法更新")

    def _update_tags_mongo(self, doc: dict[str, Any], tags: list[str]) -> bool:
        coll = self._mongo_coll
        if coll is None:
            return False
        filt = self._mongo_filter_for_doc(doc)
        result = coll.update_one(filt, {"$set": {"tags": tags}})
        if doc.get("is_agri_related"):
            try:
                from src.core.config import get_settings
                from src.db.mongo_client import get_mongo_database

                settings = get_settings()
                db = get_mongo_database(settings.mongodb_uri, settings.mongodb_db)
                if db is not None:
                    db["tagged_documents"].update_one(filt, {"$set": {"tags": tags}})
            except Exception:
                logger.debug("同步 tagged_documents 标签失败", exc_info=True)
        return bool(result.matched_count)

    def _update_tags_jsonl(self, notice_id: str, tags: list[str]) -> bool:
        docs = self._load_jsonl()
        updated = False
        for doc in docs:
            if self._doc_matches_id(doc, notice_id):
                doc["tags"] = tags
                updated = True
                break
        if not updated:
            return False
        with self._jsonl_path.open("w", encoding="utf-8") as f:
            for doc in docs:
                f.write(json.dumps(doc, ensure_ascii=False, default=str) + "\n")
        self._jsonl_cache = None
        self._jsonl_mtime = 0.0
        return True

    def update_notice_tags(self, notice_id: str, tags: list[str]) -> Optional[dict[str, Any]]:
        """更新公告 tags 并持久化到 Mongo 或 JSONL。"""
        raw = self._find_raw_doc(notice_id)
        if not raw:
            return None
        if not self._in_mvp_scope(raw):
            return None

        merged = persistable_notice_tags(raw, tags)
        if self._mongo_available and self._mongo_coll is not None:
            if not self._update_tags_mongo(raw, merged):
                return None
        elif not self._update_tags_jsonl(notice_id, merged):
            return None

        return self.get_notice(notice_id)


_service: Optional[NoticeDataService] = None


def get_data_service() -> NoticeDataService:
    global _service
    if _service is None:
        _service = NoticeDataService()
    return _service
