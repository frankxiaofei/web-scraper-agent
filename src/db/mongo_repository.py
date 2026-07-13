"""统一 MongoDB 仓储：bid_notices / crawl_visited / crawl_sessions。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from src.core.models import BidNotice
from src.core.url_utils import is_likely_detail_url, url_backfill_skip_reason

logger = logging.getLogger(__name__)


def _notice_to_doc(notice: BidNotice, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    doc = notice.model_dump(mode="json")
    doc["source"] = notice.source_site_id
    doc["site_id"] = notice.source_site_id
    doc["crawled_at"] = notice.scraped_at
    if notice.category:
        doc["category"] = notice.category.value
    if notice.page_type:
        doc["page_type"] = notice.page_type.value
    if extra:
        doc.update(extra)
    return doc


class MongoRepository:
    """MongoDB 统一仓储。"""

    BID_NOTICES = "bid_notices"
    BIM_NOTICES = "bim_notices"
    TAGGED_DOCUMENTS = "tagged_documents"
    CRAWL_VISITED = "crawl_visited"
    CRAWL_SESSIONS = "crawl_sessions"

    def __init__(
        self,
        uri: str,
        db_name: str = "bid_scraper",
        collection_name: str = "bid_notices",
    ) -> None:
        self._uri = uri
        self._db_name = db_name
        self._notices_collection_name = collection_name
        self._client = None
        self._db = None
        self._notices = None
        self._bim_notices = None
        self._visited = None
        self._sessions = None
        self._tagged_documents = None
        self._available = False
        self._connect()

    def _connect(self) -> None:
        try:
            from src.db.mongo_client import get_mongo_database

            db = get_mongo_database(self._uri, self._db_name)
            if db is None:
                self._available = False
                return
            self._db = db
            self._notices = self._db[self._notices_collection_name]
            self._bim_notices = self._db[self.BIM_NOTICES]
            self._visited = self._db[self.CRAWL_VISITED]
            self._sessions = self._db[self.CRAWL_SESSIONS]
            self._tagged_documents = self._db[self.TAGGED_DOCUMENTS]
            self._ensure_indexes()
            self._available = True
            logger.info("MongoDB 仓储已连接: %s", self._db_name)
        except BaseException as e:
            logger.warning("MongoDB 不可用，已降级为 JSONL: %s", e)
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def _ensure_indexes(self) -> None:
        if self._notices is not None:
            self._notices.create_index("source")
            self._notices.create_index("site_id")
            self._notices.create_index("content_hash", unique=True)
            self._notices.create_index([("crawled_at", -1)])
            self._notices.create_index([("publish_date", -1)])
            self._notices.create_index("depth")
            self._notices.create_index("parent_url")
            self._notices.create_index("page_type")
            self._notices.create_index("is_bim_related")
            self._notices.create_index("tags")
        if self._bim_notices is not None:
            self._bim_notices.create_index("source")
            self._bim_notices.create_index("site_id")
            self._bim_notices.create_index("content_hash", unique=True)
            self._bim_notices.create_index([("crawled_at", -1)])
            self._bim_notices.create_index([("publish_date", -1)])
        if self._visited is not None:
            self._visited.create_index([("site_id", 1), ("url", 1)], unique=True)
            self._visited.create_index([("visited_at", -1)])
        if self._sessions is not None:
            self._sessions.create_index("session_id", unique=True)
            self._sessions.create_index([("site_id", 1), ("started_at", -1)])
        if self._tagged_documents is not None:
            self._tagged_documents.create_index("tags")
            self._tagged_documents.create_index("site_id")
            self._tagged_documents.create_index("content_hash", unique=True)
            self._tagged_documents.create_index("url", unique=True, sparse=True)
            self._tagged_documents.create_index([("created_at", -1)])

    def check_connection(self) -> bool:
        if not self._available:
            return False
        try:
            from src.db.mongo_client import mongo_is_available

            return mongo_is_available()
        except Exception:
            return False

    # ── bid_notices ──────────────────────────────────────────

    _BIM_PRESERVE_IF_UNSET = (
        "is_bim_related",
        "bim_confidence",
        "bim_reason",
        "bim_tags",
        "bim_classified_at",
    )

    def upsert_notice(self, notice: BidNotice, extra: Optional[dict[str, Any]] = None) -> bool:
        if not self._available or self._notices is None or not notice.content_hash:
            return False
        now = datetime.now(timezone.utc)
        doc = _notice_to_doc(notice, extra)
        doc["updated_at"] = now
        if notice.is_bim_related is None:
            for key in self._BIM_PRESERVE_IF_UNSET:
                doc.pop(key, None)
        try:
            result = self._notices.update_one(
                {"content_hash": notice.content_hash},
                {"$set": doc, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
            if notice.is_bim_related is True:
                self._upsert_bim_notice(doc, now)
            return bool(result.upserted_id or result.modified_count)
        except Exception as e:
            logger.warning("MongoDB upsert 失败 %s: %s", notice.url, e)
            return False

    def _upsert_bim_notice(self, doc: dict[str, Any], now: datetime) -> None:
        if not self._available or self._bim_notices is None:
            return
        content_hash = doc.get("content_hash")
        if not content_hash:
            return
        try:
            self._bim_notices.update_one(
                {"content_hash": content_hash},
                {"$set": doc, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
        except Exception as e:
            logger.warning("MongoDB bim_notices upsert 失败 %s: %s", content_hash, e)

    def count_bim_by_source(self) -> dict[str, int]:
        """统计各站点 BIM 相关公告总数（is_bim_related=true）。"""
        if not self._available or self._notices is None:
            return {}
        pipeline = [
            {"$match": {"is_bim_related": True}},
            {
                "$group": {
                    "_id": {
                        "$ifNull": ["$source_site_id", {"$ifNull": ["$source", "$site_id"]}]
                    },
                    "count": {"$sum": 1},
                }
            },
        ]
        return {
            str(row["_id"]): int(row["count"])
            for row in self._notices.aggregate(pipeline)
            if row.get("_id")
        }

    def upsert_many(self, notices: list[BidNotice]) -> int:
        count = 0
        for notice in notices:
            if self.upsert_notice(notice):
                count += 1
        if count:
            logger.info("MongoDB upsert %d 条公告", count)
        return count

    def find_by_source(self, source: str, limit: int = 10) -> list[dict[str, Any]]:
        if not self._available or self._notices is None:
            return []
        cursor = (
            self._notices.find({"$or": [{"source": source}, {"site_id": source}]})
            .sort("crawled_at", -1)
            .limit(limit)
        )
        return list(cursor)

    def find_missing_content(
        self,
        source: Optional[str] = None,
        limit: int = 10,
        *,
        detail_only: bool = True,
    ) -> list[dict[str, Any]]:
        """查询缺少正文的公告（content_text 与 content_html 均为空）。

        detail_only=True 时仅返回疑似详情页 URL，排除 index/list/栏目导航等。
        """
        if not self._available or self._notices is None:
            return []
        query: dict[str, Any] = {
            "$and": [
                {"$or": [{"content_text": {"$exists": False}}, {"content_text": None}, {"content_text": ""}]},
                {"$or": [{"content_html": {"$exists": False}}, {"content_html": None}, {"content_html": ""}]},
            ]
        }
        if source:
            query = {
                "$and": [
                    query,
                    {"$or": [{"source": source}, {"site_id": source}, {"source_site_id": source}]},
                ]
            }
        fetch_limit = limit * 10 if detail_only else limit
        cursor = self._notices.find(query).sort("crawled_at", -1).limit(fetch_limit)
        if not detail_only:
            return list(cursor)

        results: list[dict[str, Any]] = []
        for doc in cursor:
            url = doc.get("url") or doc.get("detail_url") or ""
            if is_likely_detail_url(url):
                results.append(doc)
                if len(results) >= limit:
                    break
        return results

    @staticmethod
    def missing_content_skip_reason(doc: dict[str, Any]) -> str | None:
        """返回该文档若用于补抓时的跳过原因。"""
        url = doc.get("url") or doc.get("detail_url") or ""
        return url_backfill_skip_reason(url)

    # ── tagged_documents ─────────────────────────────────────

    def upsert_tagged_document(
        self,
        *,
        title: str,
        tags: list[str],
        url: Optional[str] = None,
        content: Optional[str] = None,
        site_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        source: Optional[str] = None,
        content_hash: str,
    ) -> dict[str, Any]:
        """写入或更新带标签文档（按 url 或 content_hash 去重）。"""
        if not self._available or self._tagged_documents is None:
            return {"ok": False, "saved": False, "error": "MongoDB 不可用"}
        now = datetime.now(timezone.utc)
        doc: dict[str, Any] = {
            "title": title,
            "tags": tags,
            "content_hash": content_hash,
            "updated_at": now,
        }
        if url:
            doc["url"] = url
        if content is not None:
            doc["content"] = content
        if site_id:
            doc["site_id"] = site_id
        if source:
            doc["source"] = source
        if metadata:
            doc["metadata"] = metadata
        filt: dict[str, Any]
        if url:
            filt = {"url": url}
        else:
            filt = {"content_hash": content_hash}
        try:
            existing = self._tagged_documents.find_one(filt, {"_id": 1})
            result = self._tagged_documents.update_one(
                filt,
                {"$set": doc, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
            doc_id = result.upserted_id or (existing or {}).get("_id")
            inserted = result.upserted_id is not None
            updated = bool(result.modified_count and not inserted)
            return {
                "ok": True,
                "saved": inserted or updated,
                "inserted": inserted,
                "updated": updated,
                "duplicate": bool(existing and not updated and not inserted),
                "document_id": str(doc_id) if doc_id else None,
            }
        except Exception as e:
            logger.warning("MongoDB tagged_documents upsert 失败 %s: %s", title[:40], e)
            return {"ok": False, "saved": False, "error": str(e)}

    def find_tagged_by_tags(
        self,
        tags: list[str],
        *,
        match_mode: str = "any",
        site_id: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """按标签检索 tagged_documents，返回摘要列表。"""
        if not self._available or self._tagged_documents is None:
            return []
        query: dict[str, Any] = {}
        if tags:
            if match_mode == "all":
                query["tags"] = {"$all": tags}
            else:
                query["tags"] = {"$in": tags}
        if site_id:
            query["site_id"] = site_id
        cursor = (
            self._tagged_documents.find(
                query,
                {
                    "_id": 1,
                    "title": 1,
                    "url": 1,
                    "tags": 1,
                    "site_id": 1,
                    "source": 1,
                    "created_at": 1,
                    "updated_at": 1,
                    "content": 1,
                },
            )
            .sort("created_at", -1)
            .limit(max(1, min(limit, 200)))
        )
        items: list[dict[str, Any]] = []
        for doc in cursor:
            content = doc.get("content") or ""
            items.append(
                {
                    "document_id": str(doc["_id"]),
                    "title": doc.get("title") or "",
                    "url": doc.get("url"),
                    "tags": doc.get("tags") or [],
                    "site_id": doc.get("site_id"),
                    "source": doc.get("source"),
                    "created_at": doc.get("created_at"),
                    "updated_at": doc.get("updated_at"),
                    "content_preview": content[:200] if content else "",
                }
            )
        return items

    # ── crawl_visited ────────────────────────────────────────

    def mark_visited(self, url: str, *, site_id: str) -> None:
        if not self._available or self._visited is None:
            return
        now = datetime.now(timezone.utc)
        try:
            self._visited.update_one(
                {"site_id": site_id, "url": url},
                {"$set": {"visited_at": now}, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
        except Exception as e:
            logger.debug("mark_visited 失败 %s: %s", url, e)

    def load_visited_urls(self, site_id: str, limit: int = 10000) -> set[str]:
        if not self._available or self._visited is None:
            return set()
        cursor = self._visited.find({"site_id": site_id}, {"url": 1}).limit(limit)
        return {doc["url"] for doc in cursor if doc.get("url")}

    def is_visited(self, url: str, *, site_id: str) -> bool:
        if not self._available or self._visited is None:
            return False
        return self._visited.find_one({"site_id": site_id, "url": url}) is not None

    # ── crawl_sessions ───────────────────────────────────────

    def start_session(
        self,
        session_id: str,
        *,
        site_id: str,
        max_depth: int,
        max_pages: int,
    ) -> None:
        if not self._available or self._sessions is None:
            return
        now = datetime.now(timezone.utc)
        self._sessions.update_one(
            {"session_id": session_id},
            {
                "$set": {
                    "site_id": site_id,
                    "max_depth": max_depth,
                    "max_pages": max_pages,
                    "pages_crawled": 0,
                    "notices_saved": 0,
                    "status": "running",
                    "started_at": now,
                }
            },
            upsert=True,
        )

    def finish_session(
        self,
        session_id: str,
        *,
        pages_crawled: int,
        notices_saved: int,
        status: str = "completed",
    ) -> None:
        if not self._available or self._sessions is None:
            return
        self._sessions.update_one(
            {"session_id": session_id},
            {
                "$set": {
                    "pages_crawled": pages_crawled,
                    "notices_saved": notices_saved,
                    "status": status,
                    "finished_at": datetime.now(timezone.utc),
                }
            },
        )

    def close(self) -> None:
        self._db = None
        self._notices = None
        self._bim_notices = None
        self._visited = None
        self._sessions = None
        self._tagged_documents = None
        self._available = False


# 向后兼容别名
MongoNoticeRepository = MongoRepository


def create_mongo_repository(
    uri: Optional[str] = None,
    db_name: str = "bid_scraper",
    collection_name: str = "bid_notices",
) -> Optional[MongoRepository]:
    if not uri:
        return None
    return MongoRepository(uri, db_name=db_name, collection_name=collection_name)


_SHARED_REPO: Optional[MongoRepository] = None
_SHARED_REPO_CHECKED_AT: float = 0.0
_SHARED_REPO_RETRY_SECONDS = 30.0
_BIM_TOTALS_CACHE: Optional[tuple[float, dict[str, int]]] = None
_BIM_TOTALS_CACHE_TTL_SECONDS = 60.0


def get_shared_mongo_repository(
    uri: Optional[str] = None,
    db_name: str = "bid_scraper",
    collection_name: str = "bid_notices",
) -> Optional[MongoRepository]:
    """进程内复用 Mongo 连接；不可用时 30s 内不再重连（避免 3s 超时拖慢 SSR）。"""
    import time

    global _SHARED_REPO, _SHARED_REPO_CHECKED_AT
    if not uri:
        return None

    now = time.monotonic()
    if _SHARED_REPO is not None:
        if _SHARED_REPO.available:
            return _SHARED_REPO
        if now - _SHARED_REPO_CHECKED_AT < _SHARED_REPO_RETRY_SECONDS:
            return _SHARED_REPO

    if _SHARED_REPO is not None:
        _SHARED_REPO.close()
    _SHARED_REPO_CHECKED_AT = now
    _SHARED_REPO = create_mongo_repository(
        uri=uri,
        db_name=db_name,
        collection_name=collection_name,
    )
    return _SHARED_REPO


def get_cached_bim_totals_by_source(
    uri: Optional[str] = None,
    db_name: str = "bid_scraper",
    collection_name: str = "bid_notices",
) -> dict[str, int]:
    """带 TTL 的 BIM 站点计数；Mongo 不可用时快速返回空 dict。"""
    import time

    global _BIM_TOTALS_CACHE
    now = time.monotonic()
    if _BIM_TOTALS_CACHE is not None:
        cached_at, cached = _BIM_TOTALS_CACHE
        if now - cached_at < _BIM_TOTALS_CACHE_TTL_SECONDS:
            return cached

    repo = get_shared_mongo_repository(
        uri=uri,
        db_name=db_name,
        collection_name=collection_name,
    )
    totals: dict[str, int] = {}
    if repo and repo.available:
        totals = repo.count_bim_by_source()
    _BIM_TOTALS_CACHE = (now, totals)
    return totals
