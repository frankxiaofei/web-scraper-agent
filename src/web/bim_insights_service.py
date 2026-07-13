"""BIM 招标公告聚合分析与洞察。"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Union

from src.core.bim_classifier import is_bim_classify_enabled
from src.core.notice_field_utils import aggregate_structured_insights, structured_fields_from_doc
from src.core.site_sync import get_crawl_scope_label, load_enabled_sites, load_sites_config
from src.core.timezone_utils import app_now, to_app_tz
from src.core.url_utils import resolve_notice_detail_url
from src.web.data_service import (
    NoticeDataService,
    NOTICE_MONGO_SORT,
    _format_dt,
    doc_crawl_timestamp,
    doc_publish_timestamp,
    get_data_service,
    notice_content_href,
    notice_id_from_doc,
)
from src.web.stats_service import _load_site_metadata, _with_pct

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
INSIGHTS_CACHE_PATH = ROOT / "data" / "bim_insights_cache.json"
DEFAULT_BIM_DAYS = 180
MAX_BIM_DAYS = 180
ANALYSIS_WINDOW_DAYS = 30


def _doc_source_id(doc: dict[str, Any]) -> str:
    return doc.get("source_site_id") or doc.get("source") or doc.get("site_id") or ""


def _doc_timestamp(doc: dict[str, Any]) -> Optional[datetime]:
    """BIM 时间窗口/排序口径：以公告发布时间为准。"""
    return doc_publish_timestamp(doc)


def _string_id(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def is_bim_candidate(doc: dict[str, Any]) -> bool:
    """BIM 简报候选：is_bim_related / bim_tags / 标题含 BIM。"""
    if doc.get("is_bim_related") is True:
        return True
    tags = doc.get("bim_tags")
    if isinstance(tags, list) and tags:
        return True
    return "BIM" in (doc.get("title") or "").upper()


def is_wechat_bim_report_source(doc: dict[str, Any]) -> bool:
    """BIM 日报/周报排除微信公众号来源。"""
    if doc.get("wechat"):
        return True
    for key in ("source_site_id", "source", "site_id"):
        value = str(doc.get(key) or "").strip()
        if value.startswith("wechat_"):
            return True
    category = str(doc.get("category") or "").strip().lower()
    return category == "wechat"


def _notice_item(doc: dict[str, Any]) -> dict[str, Any]:
    pub_ts = doc_publish_timestamp(doc)
    crawl_ts = doc_crawl_timestamp(doc)
    structured = structured_fields_from_doc(doc, use_llm=False)
    notice_id = notice_id_from_doc(doc)
    resolved_url = resolve_notice_detail_url(
        doc.get("url") or doc.get("detail_url") or "",
        base_url=doc.get("source_url") or "",
    )
    return {
        "id": notice_id,
        "content_href": notice_content_href(notice_id),
        "title": doc.get("title") or "",
        "url": resolved_url,
        "source_site_id": _doc_source_id(doc),
        "source_site_name": doc.get("source_site_name") or _doc_source_id(doc),
        "category": doc.get("category") or "未分类",
        "is_bim_related": doc.get("is_bim_related"),
        "bim_confidence": doc.get("bim_confidence"),
        "bim_reason": doc.get("bim_reason") or "",
        "bim_tags": doc.get("bim_tags") or [],
        "publish_date": pub_ts.isoformat() if pub_ts else None,
        "publish_date_fmt": _format_dt(pub_ts) if pub_ts else "—",
        "scraped_at": crawl_ts.isoformat() if crawl_ts else None,
        "scraped_at_fmt": _format_dt(crawl_ts) if crawl_ts else "—",
        "key_summary": (doc.get("key_summary") or structured.get("key_summary") or "")[:160],
        "budget_amount": structured.get("budget_amount") or structured.get("amount_display"),
        "tender_party": structured.get("tender_party"),
        "bid_deadline": structured.get("bid_deadline"),
        "project_location": structured.get("project_location"),
    }


DateInput = Union[date, datetime, str, None]


class BimInsightsService:
    """BIM 公告统计、最新列表与可选 LLM 分析。"""

    def __init__(self, data_service: Optional[NoticeDataService] = None) -> None:
        self._data = data_service or get_data_service()

    def _enabled_site_ids(self) -> list[str]:
        return [s["id"] for s in load_enabled_sites() if s.get("id")]

    @staticmethod
    def _bim_brief_mongo_clause() -> dict[str, Any]:
        """BIM 简报/周报 Mongo 子句：与 is_bim_candidate 口径一致。"""
        return {
            "$or": [
                {"is_bim_related": True},
                {"bim_tags.0": {"$exists": True}},
                {"title": {"$regex": r"BIM", "$options": "i"}},
            ]
        }

    def _mongo_query(
        self,
        enabled_ids: list[str],
        *,
        bim_only: bool,
        bim_brief: bool,
    ) -> dict[str, Any]:
        """组合 MVP 站点范围与 BIM 筛选（避免覆盖 _mvp_source_filter_mongo 的 $or）。"""
        scope = self._data._mvp_source_filter_mongo(enabled_ids)  # noqa: SLF001
        if bim_brief:
            return {"$and": [scope, self._bim_brief_mongo_clause()]}
        if bim_only:
            return {"$and": [scope, {"is_bim_related": True}]}
        return scope

    def load_bim_report_docs(
        self,
        *,
        days: int,
        site_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """日报/周报共用的 BIM 候选文档（is_bim_related / bim_tags / 标题含 BIM）。"""
        docs = self._load_docs(days=days, bim_brief=True, site_id=site_id)
        return [d for d in docs if not is_wechat_bim_report_source(d)]

    def _load_docs(
        self,
        *,
        days: int = DEFAULT_BIM_DAYS,
        bim_only: bool = True,
        bim_brief: bool = False,
        site_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        days = max(1, min(days, MAX_BIM_DAYS))
        cutoff = datetime.now() - timedelta(days=days)
        enabled_ids = set(self._enabled_site_ids())
        if site_id:
            enabled_ids = {site_id} if site_id in enabled_ids else set()

        if self._data.data_source == "mongodb" and self._data._mongo_coll is not None:
            coll = self._data._mongo_coll
            query = self._mongo_query(
                list(enabled_ids),
                bim_only=bim_only,
                bim_brief=bim_brief,
            )
            docs: list[dict[str, Any]] = []
            for doc in coll.find(query).sort(NOTICE_MONGO_SORT):
                ts = _doc_timestamp(doc)
                if ts is not None and ts < cutoff:
                    continue
                if bim_brief and not is_bim_candidate(doc):
                    continue
                docs.append(doc)
            return docs

        docs = []
        for doc in self._data._load_jsonl():  # noqa: SLF001
            if not self._data._in_mvp_scope(doc):  # noqa: SLF001
                continue
            sid = _doc_source_id(doc)
            if site_id and sid != site_id:
                continue
            if bim_brief:
                if not is_bim_candidate(doc):
                    continue
            elif bim_only and doc.get("is_bim_related") is not True:
                continue
            ts = _doc_timestamp(doc)
            if ts is not None and ts < cutoff:
                continue
            docs.append(doc)
        docs.sort(
            key=lambda d: _doc_timestamp(d) or datetime.min,
            reverse=True,
        )
        return docs

    def get_counts(self) -> dict[str, Any]:
        """MVP 站点 BIM 分类计数。"""
        enabled_ids = self._enabled_site_ids()
        if self._data.data_source == "mongodb" and self._data._mongo_coll is not None:
            coll = self._data._mongo_coll
            base = self._data._mvp_source_filter_mongo(enabled_ids)
            counts = {
                "total": coll.count_documents(base),
                "bim_true": coll.count_documents({**base, "is_bim_related": True}),
                "bim_false": coll.count_documents({**base, "is_bim_related": False}),
                "bim_unclassified": coll.count_documents(
                    {
                        "$and": [
                            base,
                            {
                                "$or": [
                                    {"is_bim_related": {"$exists": False}},
                                    {"is_bim_related": None},
                                ]
                            },
                        ]
                    }
                ),
            }
            counts["bim_classified"] = counts["bim_true"] + counts["bim_false"]
            return counts

        total = bim_true = bim_false = bim_unclassified = 0
        for doc in self._data._load_jsonl():  # noqa: SLF001
            if not self._data._in_mvp_scope(doc):  # noqa: SLF001
                continue
            total += 1
            rel = doc.get("is_bim_related")
            if rel is True:
                bim_true += 1
            elif rel is False:
                bim_false += 1
            else:
                bim_unclassified += 1
        return {
            "total": total,
            "bim_true": bim_true,
            "bim_false": bim_false,
            "bim_unclassified": bim_unclassified,
            "bim_classified": bim_true + bim_false,
        }

    def get_latest(
        self,
        *,
        limit: int = 20,
        days: int = DEFAULT_BIM_DAYS,
        site_id: Optional[str] = None,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        docs = self._load_docs(days=days, bim_only=True, site_id=site_id)
        items = [_notice_item(d) for d in docs[:limit]]
        return {
            "days": days,
            "total_in_window": len(docs),
            "items": items,
            "bim_classify_llm": is_bim_classify_enabled(),
        }

    def get_summary(
        self,
        *,
        days: int = DEFAULT_BIM_DAYS,
        site_id: Optional[str] = None,
    ) -> dict[str, Any]:
        docs = self._load_docs(days=days, bim_only=True, site_id=site_id)
        counts = self.get_counts()
        yaml_names, yaml_companies = _load_site_metadata()

        site_counter: Counter[str] = Counter()
        site_names: dict[str, str] = {}
        category_counter: Counter[str] = Counter()
        tag_counter: Counter[str] = Counter()
        daily_counts: dict[str, int] = defaultdict(int)

        for doc in docs:
            sid = _doc_source_id(doc)
            site_counter[sid] += 1
            site_names[sid] = doc.get("source_site_name") or yaml_names.get(sid) or sid
            category_counter[str(doc.get("category") or "未分类")] += 1
            for tag in doc.get("bim_tags") or []:
                if tag:
                    tag_counter[str(tag)] += 1
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
        by_category = _with_pct(
            [{"name": name, "count": cnt} for name, cnt in category_counter.items()],
            total=total,
        )
        hot_tags = [
            {"tag": tag, "count": cnt}
            for tag, cnt in tag_counter.most_common(12)
        ]
        daily_series = [
            {"date": key, "count": daily_counts.get(key, 0)}
            for key in sorted(daily_counts.keys(), reverse=True)[:14]
        ]

        return {
            "days": days,
            "bim_count_in_window": total,
            "counts": counts,
            "by_site": by_site,
            "by_category": by_category,
            "hot_tags": hot_tags,
            "daily_trend": daily_series,
            "generated_at": app_now().isoformat(),
        }

    def get_structured_summary(
        self,
        *,
        days: int = DEFAULT_BIM_DAYS,
        site_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """价格、招标人、资质要求等结构化聚合。"""
        docs = self._load_docs(days=days, bim_only=True, site_id=site_id)
        return aggregate_structured_insights(docs)

    def _generate_llm_analysis(
        self,
        summary: dict[str, Any],
        latest_items: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        from src.core.llm_chat import LLMChatClient

        client = LLMChatClient()
        if not client.available:
            return None

        payload = {
            "window_days": summary.get("days"),
            "bim_count": summary.get("bim_count_in_window"),
            "by_site": summary.get("by_site", [])[:8],
            "hot_tags": summary.get("hot_tags", [])[:10],
            "sample_titles": [i.get("title") for i in latest_items[:15]],
            "structured": {
                "price_buckets": (summary.get("structured") or {}).get("price_buckets", [])[:6],
                "top_tender_parties": (summary.get("structured") or {}).get("top_tender_parties", [])[:5],
                "requirement_keywords": (summary.get("structured") or {}).get("requirement_keywords", [])[:8],
                "amount_stats": (summary.get("structured") or {}).get("amount_stats", {}),
            },
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 BIM 招标情报分析师。根据近一月（约30天）BIM 标讯统计数据与样本标题，"
                    "输出 JSON：trends(数组,2-4条趋势), hot_keywords(数组,5-8个), "
                    "key_projects(数组,3-5条重点标段/项目简述), summary(80字内总述), "
                    "price_insights(数组,1-3条价格/预算区间观察), "
                    "requirement_insights(数组,1-3条资质/招标要求观察)。"
                    "只输出合法 JSON，不要编造不存在的信息。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ]
        content, err = client.chat(messages, temperature=0.2, timeout=60)
        if err or not content:
            logger.warning("BIM 洞察 LLM 失败: %s", err)
            from src.core.llm_alert_service import maybe_alert_on_llm_error

            maybe_alert_on_llm_error(err or "LLM 返回空内容", source="bim_insights_llm_analysis")
            return None
        text = content.strip()
        if "```" in text:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                text = text[start:end]
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("BIM 洞察 LLM 返回非 JSON")
            return {"summary": content[:500]}
        return {
            "trends": data.get("trends") or [],
            "hot_keywords": data.get("hot_keywords") or [],
            "key_projects": data.get("key_projects") or [],
            "summary": data.get("summary") or "",
            "price_insights": data.get("price_insights") or [],
            "requirement_insights": data.get("requirement_insights") or [],
        }

    def get_period_new_counts(self, reference: DateInput = None) -> dict[str, Any]:
        """本周/近一月（滚动 30 天）新增 BIM 候选计数（与周报口径一致）。"""
        from src.core.bim_daily_brief_service import compute_period_new_counts

        return compute_period_new_counts(reference, insights_service=self)

    def get_insights(
        self,
        *,
        days: int = DEFAULT_BIM_DAYS,
        use_llm: bool = False,
        site_id: Optional[str] = None,
        refresh_llm: bool = False,
    ) -> dict[str, Any]:
        summary = self.get_summary(days=days, site_id=site_id)
        structured = self.get_structured_summary(days=days, site_id=site_id)
        summary["structured"] = structured
        summary.update(self.get_period_new_counts())
        latest = self.get_latest(limit=15, days=days, site_id=site_id)

        analysis_days = ANALYSIS_WINDOW_DAYS
        analysis_summary = self.get_summary(days=analysis_days, site_id=site_id)
        analysis_structured = self.get_structured_summary(
            days=analysis_days, site_id=site_id
        )
        analysis_summary["structured"] = analysis_structured

        result: dict[str, Any] = {
            **summary,
            "latest": latest.get("items") or [],
            "llm_analysis": None,
            "analysis_days": analysis_days,
            "analysis_bim_count": analysis_summary.get("bim_count_in_window", 0),
        }

        cached = _read_cache()
        cache_key = f"analysis:{analysis_days}:{site_id or 'all'}"
        if (
            use_llm
            and cached
            and cached.get("cache_key") == cache_key
            and cached.get("llm_analysis")
            and not refresh_llm
        ):
            result["llm_analysis"] = cached.get("llm_analysis")
            result["llm_cached"] = True
            return result

        if use_llm:
            latest_for_analysis = self.get_latest(
                limit=15, days=analysis_days, site_id=site_id
            )
            analysis = self._generate_llm_analysis(
                analysis_summary, latest_for_analysis.get("items") or []
            )
            result["llm_analysis"] = analysis
            if analysis:
                _write_cache(
                    {
                        "cache_key": cache_key,
                        "generated_at": app_now().isoformat(),
                        "llm_analysis": analysis,
                        "summary": analysis_summary,
                    }
                )
        return result

    def get_page_context(
        self, *, days: int = DEFAULT_BIM_DAYS, use_llm: bool = True
    ) -> dict[str, Any]:
        insights = self.get_insights(days=days, use_llm=use_llm)
        latest_data = self.get_latest(limit=30, days=days)
        from src.core.site_sync import load_enabled_sites

        mvp_sites = [
            {"id": s["id"], "name": s.get("name") or s["id"], "enabled": True}
            for s in load_enabled_sites()
        ]
        return {
            "insights": insights,
            "latest_notices": latest_data.get("items") or [],
            "latest_total": latest_data.get("total_in_window") or 0,
            "mvp_sites": mvp_sites,
            "crawl_scope_label": get_crawl_scope_label(),
            "days": days,
        }


def _read_cache() -> Optional[dict[str, Any]]:
    if not INSIGHTS_CACHE_PATH.exists():
        return None
    try:
        return json.loads(INSIGHTS_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(data: dict[str, Any]) -> None:
    INSIGHTS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    INSIGHTS_CACHE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


_service: Optional[BimInsightsService] = None


def get_bim_insights_service() -> BimInsightsService:
    global _service
    if _service is None:
        _service = BimInsightsService()
    return _service
