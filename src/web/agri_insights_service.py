"""智慧农业招标洞察聚合分析。"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from src.core.agri_classifier import (
    agri_relevance_score,
    doc_is_agri_related,
    infer_agri_application_scenes,
    infer_agri_notice_type,
    infer_agri_tech_types,
    matched_agri_keywords,
    score_agri_opportunity,
)
from src.core.agri_sites import agri_site_display_list, get_agri_scope_label, get_agri_site_ids
from src.core.notice_field_utils import aggregate_structured_insights, structured_fields_from_doc
from src.core.timezone_utils import app_now, to_app_tz
from src.core.url_utils import resolve_notice_detail_url
from src.web.data_service import (
    NOTICE_MONGO_SORT,
    NoticeDataService,
    _format_dt,
    doc_publish_timestamp,
    get_data_service,
    notice_content_href,
    notice_id_from_doc,
)
from src.web.stats_service import _load_site_metadata, _with_pct

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
AGRI_UI_PORT_PATH = ROOT / "data" / ".agri_ui_port"
MAIN_UI_PORT_PATH = ROOT / "data" / ".web_ui_port"
INSIGHTS_CACHE_PATH = ROOT / "data" / "agri_insights_cache.json"
DEFAULT_AGRI_DAYS = 30
MAX_AGRI_DAYS = 90
_INSIGHTS_CACHE_TTL_SECONDS = 60.0
_DOCS_PROJECTION = {"content_html": 0, "content_text": 0, "attachments": 0}

_service: Optional["AgriInsightsService"] = None
_docs_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_summary_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def get_agri_ui_port(default: int = 8091) -> int:
    if AGRI_UI_PORT_PATH.exists():
        try:
            return int(AGRI_UI_PORT_PATH.read_text(encoding="utf-8").strip())
        except ValueError:
            pass
    return default


def get_main_ui_port(default: int = 8090) -> int:
    if MAIN_UI_PORT_PATH.exists():
        try:
            return int(MAIN_UI_PORT_PATH.read_text(encoding="utf-8").strip())
        except ValueError:
            pass
    return default


def is_agri_related(doc: dict[str, Any]) -> bool:
    return doc_is_agri_related(doc)


def _doc_source_id(doc: dict[str, Any]) -> str:
    return doc.get("source_site_id") or doc.get("source") or doc.get("site_id") or ""


def _doc_timestamp(doc: dict[str, Any]) -> Optional[datetime]:
    return doc_publish_timestamp(doc)


def _notice_item(doc: dict[str, Any]) -> dict[str, Any]:
    pub_ts = doc_publish_timestamp(doc)
    structured = structured_fields_from_doc(doc, use_llm=False)
    notice_id = notice_id_from_doc(doc)
    kws = matched_agri_keywords(doc)
    opp = score_agri_opportunity(doc)
    return {
        "id": notice_id,
        "content_href": notice_content_href(notice_id),
        "title": doc.get("title") or "",
        "url": resolve_notice_detail_url(
            doc.get("url") or doc.get("detail_url") or "",
            base_url=doc.get("source_url") or "",
        ),
        "source_site_id": _doc_source_id(doc),
        "source_site_name": doc.get("source_site_name") or _doc_source_id(doc),
        "category": doc.get("category") or "未分类",
        "notice_type": infer_agri_notice_type(doc),
        "agri_keywords": kws,
        "tech_types": infer_agri_tech_types(doc),
        "application_scenes": infer_agri_application_scenes(doc),
        "agri_relevance": round(agri_relevance_score(doc), 2),
        "opportunity_score": opp["score"],
        "opportunity_tier": opp["tier"],
        "publish_date_fmt": _format_dt(pub_ts) if pub_ts else "—",
        "key_summary": (doc.get("key_summary") or structured.get("key_summary") or "")[:160],
        "budget_amount": structured.get("budget_amount") or structured.get("amount_display"),
        "tender_party": structured.get("tender_party"),
        "region": structured.get("project_location") or doc.get("region"),
    }



class AgriInsightsService:
    """智慧农业公告统计与商机列表聚合。"""

    def __init__(self, data_service: Optional[NoticeDataService] = None) -> None:
        self._data = data_service or get_data_service()

    def _agri_site_ids(self) -> list[str]:
        return get_agri_site_ids()

    def _load_docs(
        self,
        *,
        days: int = DEFAULT_AGRI_DAYS,
        site_id: Optional[str] = None,
        agri_only: bool = True,
    ) -> list[dict[str, Any]]:
        days = max(1, min(days, MAX_AGRI_DAYS))
        cutoff = datetime.now() - timedelta(days=days)
        allowed = set(self._agri_site_ids())
        if site_id:
            allowed = {site_id} if site_id in allowed else set()

        cache_key = f"{days}:{site_id or 'all'}:{agri_only}"
        now = time.monotonic()
        cached = _docs_cache.get(cache_key)
        if cached and now - cached[0] < _INSIGHTS_CACHE_TTL_SECONDS:
            return cached[1]

        if self._data.data_source == "mongodb" and self._data._mongo_coll is not None:
            coll = self._data._mongo_coll
            query: dict[str, Any] = {}
            if allowed:
                query["$or"] = [
                    {"source_site_id": {"$in": list(allowed)}},
                    {"source": {"$in": list(allowed)}},
                    {"site_id": {"$in": list(allowed)}},
                ]
            if agri_only:
                agri_clause = {
                    "$or": [
                        {"is_agri_related": True},
                        {"agri_tags": {"$exists": True, "$ne": []}},
                    ]
                }
                if "$and" in query:
                    query["$and"].append(agri_clause)
                elif query:
                    query = {"$and": [query, agri_clause]}
                else:
                    query = agri_clause
            docs: list[dict[str, Any]] = []
            for doc in coll.find(query, _DOCS_PROJECTION).sort(NOTICE_MONGO_SORT):
                if allowed and _doc_source_id(doc) not in allowed:
                    continue
                ts = _doc_timestamp(doc)
                if ts is not None and ts < cutoff:
                    break
                if agri_only and not doc_is_agri_related(doc):
                    continue
                docs.append(doc)
            _docs_cache[cache_key] = (now, docs)
            return docs

        docs: list[dict[str, Any]] = []
        for doc in self._data._load_jsonl():  # noqa: SLF001
            sid = _doc_source_id(doc)
            if allowed and sid not in allowed:
                continue
            if site_id and sid != site_id:
                continue
            ts = _doc_timestamp(doc)
            if ts is not None and ts < cutoff:
                continue
            if agri_only and not doc_is_agri_related(doc):
                continue
            docs.append(doc)
        docs.sort(key=lambda d: _doc_timestamp(d) or datetime.min, reverse=True)
        _docs_cache[cache_key] = (now, docs)
        return docs

    def get_counts(self) -> dict[str, Any]:
        allowed = set(self._agri_site_ids())
        if self._data.data_source == "mongodb" and self._data._mongo_coll is not None:
            coll = self._data._mongo_coll
            base = {"source_site_id": {"$in": list(allowed)}} if allowed else {}
            return {
                "total": coll.count_documents(base),
                "agri_true": coll.count_documents({**base, "is_agri_related": True}),
                "agri_false": coll.count_documents({**base, "is_agri_related": False}),
                "agri_unclassified": coll.count_documents(
                    {
                        "$and": [
                            base,
                            {
                                "$or": [
                                    {"is_agri_related": {"$exists": False}},
                                    {"is_agri_related": None},
                                ]
                            },
                        ]
                    }
                ),
            }
        total = agri_true = agri_false = agri_unclassified = 0
        for doc in self._data._load_jsonl():  # noqa: SLF001
            if allowed and _doc_source_id(doc) not in allowed:
                continue
            total += 1
            rel = doc.get("is_agri_related")
            if rel is True:
                agri_true += 1
            elif rel is False:
                agri_false += 1
            else:
                agri_unclassified += 1
        return {
            "total": total,
            "agri_true": agri_true,
            "agri_false": agri_false,
            "agri_unclassified": agri_unclassified,
        }

    def get_latest(
        self,
        *,
        limit: int = 20,
        days: int = DEFAULT_AGRI_DAYS,
        site_id: Optional[str] = None,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        docs = self._load_docs(days=days, site_id=site_id)
        return {
            "days": days,
            "total_in_window": len(docs),
            "items": [_notice_item(d) for d in docs[:limit]],
        }

    def get_summary(
        self,
        *,
        days: int = DEFAULT_AGRI_DAYS,
        site_id: Optional[str] = None,
    ) -> dict[str, Any]:
        cache_key = f"{days}:{site_id or 'all'}"
        now = time.monotonic()
        cached = _summary_cache.get(cache_key)
        if cached and now - cached[0] < _INSIGHTS_CACHE_TTL_SECONDS:
            return cached[1]

        docs = self._load_docs(days=days, site_id=site_id)
        all_docs = self._load_docs(days=days, site_id=site_id, agri_only=False)
        agri_true = sum(1 for d in all_docs if doc_is_agri_related(d))
        counts = {
            "total": len(all_docs),
            "agri_true": agri_true,
            "agri_false": max(0, len(all_docs) - agri_true),
            "agri_unclassified": 0,
        }
        yaml_names, yaml_companies = _load_site_metadata()
        site_counter: Counter[str] = Counter()
        site_names: dict[str, str] = {}
        keyword_counter: Counter[str] = Counter()
        daily_counts: dict[str, int] = defaultdict(int)

        for doc in docs:
            sid = _doc_source_id(doc)
            site_counter[sid] += 1
            site_names[sid] = doc.get("source_site_name") or yaml_names.get(sid) or sid
            for kw in matched_agri_keywords(doc):
                keyword_counter[kw] += 1
            ts = _doc_timestamp(doc)
            if ts:
                daily_counts[to_app_tz(ts).strftime("%Y-%m-%d")] += 1

        total = len(docs)
        by_site = _with_pct(
            [
                {
                    "id": sid,
                    "name": site_names.get(sid) or yaml_names.get(sid) or sid,
                    "company_name": yaml_companies.get(sid) or "",
                    "count": cnt,
                }
                for sid, cnt in site_counter.items()
            ],
            total=total,
        )
        hot_keywords = [
            {"keyword": kw, "count": cnt}
            for kw, cnt in keyword_counter.most_common(12)
        ]
        result = {
            "days": days,
            "agri_count_in_window": total,
            "counts": counts,
            "by_site": by_site,
            "hot_keywords": hot_keywords,
            "scope_label": get_agri_scope_label(),
            "generated_at": app_now().isoformat(),
        }
        _summary_cache[cache_key] = (now, result)
        return result

    def get_tenders(
        self,
        *,
        days: int = DEFAULT_AGRI_DAYS,
        limit: int = 50,
        offset: int = 0,
        site_id: Optional[str] = None,
        keyword: Optional[str] = None,
        region: Optional[str] = None,
        category: Optional[str] = None,
        min_score: int = 0,
        tier: Optional[str] = None,
        sort: str = "score",
    ) -> dict[str, Any]:
        docs = self._load_docs(days=days, site_id=site_id)
        items: list[dict[str, Any]] = []
        for doc in docs:
            item = _notice_item(doc)
            opp = score_agri_opportunity(doc)
            item["opportunity_score"] = opp["score"]
            item["opportunity_tier"] = opp["tier"]
            if min_score and opp["score"] < min_score:
                continue
            if tier and opp["tier"] != tier:
                continue
            if keyword and keyword not in (doc.get("title") or "") and keyword not in (
                doc.get("content_text") or ""
            ):
                continue
            if region and region not in str(item.get("region") or ""):
                continue
            if category and category not in str(doc.get("category") or ""):
                continue
            items.append(item)

        if sort == "date":
            items.sort(key=lambda x: x.get("publish_date_fmt") or "", reverse=True)
        else:
            items.sort(key=lambda x: x.get("opportunity_score") or 0, reverse=True)

        page = items[offset : offset + limit]
        return {
            "days": days,
            "total": len(items),
            "items": page,
            "filters": {
                "keyword": keyword,
                "region": region,
                "category": category,
                "min_score": min_score,
                "tier": tier,
                "site_id": site_id,
                "sort": sort,
            },
        }

    def get_insights(
        self,
        *,
        days: int = DEFAULT_AGRI_DAYS,
        site_id: Optional[str] = None,
    ) -> dict[str, Any]:
        summary = self.get_summary(days=days, site_id=site_id)
        structured = aggregate_structured_insights(self._load_docs(days=days, site_id=site_id))
        summary["structured"] = structured
        latest = self.get_latest(limit=15, days=days, site_id=site_id)
        return {
            **summary,
            "latest": latest.get("items") or [],
        }

    def get_page_context(
        self,
        *,
        days: int = DEFAULT_AGRI_DAYS,
    ) -> dict[str, Any]:
        insights = self.get_insights(days=days)
        latest_items = insights.get("latest") or []
        if len(latest_items) < 30:
            latest_data = self.get_latest(limit=30, days=days)
            latest_items = latest_data.get("items") or []
            latest_total = latest_data.get("total_in_window") or 0
        else:
            latest_total = insights.get("agri_count_in_window") or len(latest_items)
        return {
            "insights": insights,
            "latest_notices": latest_items[:30],
            "latest_total": latest_total,
            "mvp_sites": agri_site_display_list(),
            "scope_label": get_agri_scope_label(),
            "days": days,
            "main_ui_port": get_main_ui_port(),
            "agri_ui_port": get_agri_ui_port(),
        }


def get_agri_insights_service() -> AgriInsightsService:
    global _service
    if _service is None:
        _service = AgriInsightsService()
    return _service


__all__ = [
    "AgriInsightsService",
    "get_agri_insights_service",
    "get_agri_ui_port",
    "get_main_ui_port",
    "is_agri_related",
    "matched_agri_keywords",
]
