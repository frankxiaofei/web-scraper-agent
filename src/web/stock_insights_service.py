"""股票领域招标/资讯洞察聚合分析。"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from src.core.stock_watch import (
    get_mock_items,
    get_stock_scope_label,
    is_stock_related,
    matched_stock_keywords,
    matched_stock_themes,
    use_mock_when_empty,
)
from src.core.notice_field_utils import structured_fields_from_doc
from src.core.timezone_utils import app_now
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
from src.web.stats_service import _with_pct

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
STOCK_UI_PORT_PATH = ROOT / "data" / ".stock_ui_port"
AGRI_UI_PORT_PATH = ROOT / "data" / ".agri_ui_port"
MAIN_UI_PORT_PATH = ROOT / "data" / ".web_ui_port"
INSIGHTS_CACHE_PATH = ROOT / "data" / "stock_insights_cache.json"
DEFAULT_STOCK_DAYS = 30
MAX_STOCK_DAYS = 90
_INSIGHTS_CACHE_TTL_SECONDS = 60.0
_DOCS_PROJECTION = {"content_html": 0, "content_text": 0, "attachments": 0}
DISCLAIMER = (
    "本模块内容仅供研究与信息整理，不构成投资建议。"
    "行情与政策信息可能存在延迟，请以官方披露为准。"
)

_service: Optional["StockInsightsService"] = None
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


def get_stock_ui_port(default: int = 8092) -> int:
    if STOCK_UI_PORT_PATH.exists():
        try:
            return int(STOCK_UI_PORT_PATH.read_text(encoding="utf-8").strip())
        except ValueError:
            pass
    return default


def _doc_source_id(doc: dict[str, Any]) -> str:
    return doc.get("source_site_id") or doc.get("source") or doc.get("site_id") or ""


def _doc_timestamp(doc: dict[str, Any]) -> Optional[datetime]:
    return doc_publish_timestamp(doc)


def _notice_item(doc: dict[str, Any]) -> dict[str, Any]:
    pub_ts = doc_publish_timestamp(doc)
    notice_id = notice_id_from_doc(doc)
    kws = matched_stock_keywords(doc)
    themes = matched_stock_themes(doc)
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
        "stock_keywords": kws,
        "themes": themes,
        "publish_date_fmt": _format_dt(pub_ts) if pub_ts else "—",
        "mock": bool(doc.get("_mock")),
    }


def _read_cache() -> dict[str, Any]:
    if not INSIGHTS_CACHE_PATH.is_file():
        return {}
    try:
        return json.loads(INSIGHTS_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_cache(payload: dict[str, Any]) -> None:
    INSIGHTS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    INSIGHTS_CACHE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class StockInsightsService:
    """股票相关公告统计、主题分布与产业发展分析。"""

    def __init__(self, data_service: Optional[NoticeDataService] = None) -> None:
        self._data = data_service or get_data_service()

    def _load_real_docs(
        self,
        *,
        days: int = DEFAULT_STOCK_DAYS,
        stock_only: bool = True,
    ) -> list[dict[str, Any]]:
        days = max(1, min(days, MAX_STOCK_DAYS))
        cutoff = datetime.now() - timedelta(days=days)
        cache_key = f"{days}:{stock_only}"
        now = time.monotonic()
        cached = _docs_cache.get(cache_key)
        if cached and now - cached[0] < _INSIGHTS_CACHE_TTL_SECONDS:
            return cached[1]

        docs: list[dict[str, Any]] = []
        if self._data.data_source == "mongodb" and self._data._mongo_coll is not None:
            coll = self._data._mongo_coll
            for doc in coll.find({}, _DOCS_PROJECTION).sort(NOTICE_MONGO_SORT):
                ts = _doc_timestamp(doc)
                if ts is not None and ts < cutoff:
                    break
                if stock_only and not is_stock_related(doc):
                    continue
                docs.append(doc)
            _docs_cache[cache_key] = (now, docs)
            return docs
        for doc in self._data._load_jsonl():  # noqa: SLF001
            ts = _doc_timestamp(doc)
            if ts is not None and ts < cutoff:
                continue
            if stock_only and not is_stock_related(doc):
                continue
            docs.append(doc)
        docs.sort(key=lambda d: _doc_timestamp(d) or datetime.min, reverse=True)
        _docs_cache[cache_key] = (now, docs)
        return docs

    def _with_mock_docs(self, docs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, str]:
        if docs:
            return docs, 0, "bid_notices_keyword_filter"
        if not use_mock_when_empty():
            return docs, 0, "bid_notices_keyword_filter"
        mock_docs = [{**m, "_mock": True} for m in get_mock_items()]
        return mock_docs, len(mock_docs), "mock_demo"

    def get_counts(self, docs: list[dict[str, Any]]) -> dict[str, Any]:
        stock_true = sum(1 for d in docs if is_stock_related(d))
        return {
            "total": len(docs),
            "stock_true": stock_true,
            "stock_false": max(0, len(docs) - stock_true),
        }

    def get_latest(
        self,
        *,
        limit: int = 20,
        days: int = DEFAULT_STOCK_DAYS,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        real_docs = self._load_real_docs(days=days)
        docs, _, _ = self._with_mock_docs(real_docs)
        return {
            "days": days,
            "total_in_window": len(docs),
            "items": [_notice_item(d) for d in docs[:limit]],
        }

    def get_summary(
        self,
        *,
        days: int = DEFAULT_STOCK_DAYS,
    ) -> dict[str, Any]:
        cache_key = str(days)
        now = time.monotonic()
        cached = _summary_cache.get(cache_key)
        if cached and now - cached[0] < _INSIGHTS_CACHE_TTL_SECONDS:
            return cached[1]

        real_docs = self._load_real_docs(days=days)
        docs, mock_count, strategy = self._with_mock_docs(real_docs)
        stock_true = sum(1 for d in docs if is_stock_related(d))
        counts = {
            "total": len(docs),
            "stock_true": stock_true,
            "stock_false": max(0, len(docs) - stock_true),
        }
        theme_counter: Counter[str] = Counter()
        keyword_counter: Counter[str] = Counter()
        for doc in docs:
            for theme in matched_stock_themes(doc):
                theme_counter[theme] += 1
            for kw in matched_stock_keywords(doc):
                keyword_counter[kw] += 1
        total = len(docs)
        by_theme = _with_pct(
            [{"name": name, "count": cnt} for name, cnt in theme_counter.items()],
            total=total,
        )
        result = {
            "days": days,
            "stock_count_in_window": total,
            "counts": counts,
            "data_strategy": strategy,
            "mock_count": mock_count,
            "by_theme": by_theme,
            "hot_keywords": [
                {"keyword": kw, "count": cnt}
                for kw, cnt in keyword_counter.most_common(12)
            ],
            "scope_label": get_stock_scope_label(),
            "generated_at": app_now().isoformat(),
        }
        _summary_cache[cache_key] = (now, result)
        return result

    def get_insights(
        self,
        *,
        days: int = DEFAULT_STOCK_DAYS,
        use_llm: bool = False,
        refresh_llm: bool = False,
    ) -> dict[str, Any]:
        summary = self.get_summary(days=days)
        latest = self.get_latest(limit=15, days=days)
        result = {**summary, "latest": latest.get("items") or [], "llm_analysis": None}
        cache_key = f"stock:{days}"
        cached = _read_cache()
        if (
            use_llm
            and cached.get("cache_key") == cache_key
            and cached.get("llm_analysis")
            and not refresh_llm
        ):
            result["llm_analysis"] = cached.get("llm_analysis")
            result["llm_cached"] = True
            return result
        if use_llm:
            from src.core.llm_chat import LLMChatClient

            client = LLMChatClient()
            if client.available:
                messages = [
                    {
                        "role": "system",
                        "content": "你是股票领域招标情报分析师，输出 JSON：summary, themes, risks。",
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "count": summary.get("stock_count_in_window"),
                                "by_theme": summary.get("by_theme", [])[:8],
                            },
                            ensure_ascii=False,
                        ),
                    },
                ]
                content, _ = client.chat(messages, temperature=0.2, timeout=60)
                if content:
                    result["llm_analysis"] = {"summary": content[:500]}
                    _write_cache(
                        {
                            "cache_key": cache_key,
                            "generated_at": app_now().isoformat(),
                            "llm_analysis": result["llm_analysis"],
                            "summary": summary,
                        }
                    )
        return result

    def refresh_cache(self) -> dict[str, Any]:
        summary = self.get_summary(days=DEFAULT_STOCK_DAYS)
        payload = {
            "cache_key": f"stock:{DEFAULT_STOCK_DAYS}",
            "generated_at": app_now().isoformat(),
            "summary": summary,
        }
        _write_cache(payload)
        return {"success": True, **summary}

    def get_page_context(
        self,
        *,
        days: int = DEFAULT_STOCK_DAYS,
        use_llm: bool = False,
        refresh_llm: bool = False,
        industry: Optional[str] = None,
    ) -> dict[str, Any]:
        insights = self.get_insights(days=days, use_llm=use_llm, refresh_llm=refresh_llm)
        latest_items = insights.get("latest") or []
        if len(latest_items) < 30:
            latest_data = self.get_latest(limit=30, days=days)
            latest_items = latest_data.get("items") or []
            latest_total = latest_data.get("total_in_window") or 0
        else:
            latest_total = insights.get("stock_count_in_window") or len(latest_items)
        analysis = None
        if industry:
            analysis = self.generate_industry_analysis(
                industry=industry,
                days=days,
                use_llm=use_llm,
            )
        return {
            "insights": insights,
            "latest_notices": latest_items[:30],
            "latest_total": latest_total,
            "industry_analysis": analysis,
            "disclaimer": DISCLAIMER,
            "days": days,
            "main_ui_port": get_main_ui_port(),
            "agri_ui_port": get_agri_ui_port(),
            "stock_ui_port": get_stock_ui_port(),
        }

    def generate_industry_analysis(
        self,
        *,
        industry: str,
        days: int = DEFAULT_STOCK_DAYS,
        use_llm: bool = True,
    ) -> dict[str, Any]:
        docs = self._load_real_docs(days=days)
        docs, _, strategy = self._with_mock_docs(docs)
        related = [
            d
            for d in docs
            if industry in matched_stock_themes(d)
            or industry in (d.get("title") or "")
            or industry in (d.get("content_text") or "")
        ]
        news_items: list[dict[str, Any]] = []
        try:
            from src.core.stock_news_fetcher import get_stock_news_fetcher

            news_items = get_stock_news_fetcher().list_news(
                limit=20, days=days, industry=industry
            ).get("items") or []
        except Exception:
            news_items = []

        analysis: dict[str, Any] = {
            "summary": f"近 {days} 天与「{industry}」相关的招采/资讯约 {len(related)} 条。",
            "drivers": [f"{industry} 政策与招采需求"],
            "risks": ["数据样本有限，需结合行情与财报验证"],
            "disclaimer": DISCLAIMER,
            "data_strategy": strategy,
            "notice_count": len(related),
            "news_count": len(news_items),
        }
        if use_llm:
            from src.core.llm_chat import LLMChatClient

            client = LLMChatClient()
            if client.available:
                content, _ = client.chat(
                    [
                        {
                            "role": "system",
                            "content": "输出产业发展分析 JSON：summary, drivers, risks, chain_notes。",
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "industry": industry,
                                    "notices": [d.get("title") for d in related[:10]],
                                    "news": [n.get("title") for n in news_items[:8]],
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ],
                    temperature=0.2,
                    timeout=60,
                )
                if content:
                    analysis["llm_summary"] = content[:800]
        return {
            "industry": industry,
            "days": days,
            "analysis": analysis,
            "related_notices": [_notice_item(d) for d in related[:10]],
            "related_news": news_items[:10],
        }

    def get_analysis(
        self,
        *,
        industry: Optional[str] = None,
        days: int = DEFAULT_STOCK_DAYS,
        use_llm: bool = True,
        refresh_llm: bool = False,
    ) -> dict[str, Any]:
        target = (industry or "综合").strip()
        result = self.generate_industry_analysis(
            industry=target,
            days=days,
            use_llm=use_llm,
        )
        return {
            "industry_analysis": result,
            "disclaimer": DISCLAIMER,
            "refresh_llm": refresh_llm,
        }

    def generate_investment_plan(
        self,
        *,
        industries: Optional[list[str]] = None,
        risk_profile: str = "balanced",
        horizon_months: int = 12,
        days: int = DEFAULT_STOCK_DAYS,
        use_llm: bool = True,
    ) -> dict[str, Any]:
        industries = [i for i in (industries or []) if str(i).strip()] or ["综合"]
        phases = [
            {
                "phase": "观察建仓",
                "months": f"1-{max(3, horizon_months // 4)}",
                "focus": industries[:2],
                "actions": ["跟踪政策与招采热度", "分散主题观察"],
            },
            {
                "phase": "主题配置",
                "months": f"{max(4, horizon_months // 4 + 1)}-{max(6, horizon_months // 2)}",
                "focus": industries,
                "actions": ["结合产业链强弱调整权重", "控制单主题敞口"],
            },
            {
                "phase": "复盘调整",
                "months": f"{max(7, horizon_months // 2 + 1)}-{horizon_months}",
                "focus": industries,
                "actions": ["季度复盘政策与基本面", "动态再平衡"],
            },
        ]
        markdown = (
            f"## {horizon_months} 个月中长线思路框架（{risk_profile}）\n\n"
            f"- 关注产业：{', '.join(industries)}\n"
            f"- 数据窗口：近 {days} 天招采/资讯\n"
            f"- 说明：{DISCLAIMER}\n"
        )
        for p in phases:
            markdown += f"\n### {p['phase']}（{p['months']} 月）\n"
            markdown += f"- 主题：{', '.join(p['focus'])}\n"
            markdown += "- " + "；".join(p["actions"]) + "\n"

        plan: dict[str, Any] = {
            "industries": industries,
            "risk_profile": risk_profile,
            "horizon_months": horizon_months,
            "phases": phases,
            "markdown": markdown,
        }
        if use_llm:
            from src.core.llm_chat import LLMChatClient

            client = LLMChatClient()
            if client.available:
                content, _ = client.chat(
                    [
                        {
                            "role": "system",
                            "content": "生成非买卖建议的投资思路框架 JSON，含 phases 数组。",
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "industries": industries,
                                    "risk_profile": risk_profile,
                                    "horizon_months": horizon_months,
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ],
                    temperature=0.3,
                    timeout=60,
                )
                if content:
                    plan["llm_notes"] = content[:600]
        return {
            "horizon_months": horizon_months,
            "plan": plan,
            "disclaimer": DISCLAIMER,
        }


def get_stock_insights_service() -> StockInsightsService:
    global _service
    if _service is None:
        _service = StockInsightsService()
    return _service


__all__ = [
    "DISCLAIMER",
    "StockInsightsService",
    "get_agri_ui_port",
    "get_main_ui_port",
    "get_stock_insights_service",
    "get_stock_ui_port",
]
