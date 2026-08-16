"""细分行业空间热力聚合服务。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from src.core.agri_classifier import score_agri_opportunity
from src.core.notice_field_utils import parse_amount_yuan, structured_fields_from_doc
from src.core.timezone_utils import app_now
from src.core.url_utils import resolve_notice_detail_url
from src.industry.pseudo_industry import (
    filter_docs_by_code,
    industry_display_name,
    parse_industry_code,
)
from src.industry.region_normalizer import (
    normalize_region_to_city,
    normalize_region_to_province,
    province_name_by_code,
    region_name_by_code,
)
from src.web.data_service import _format_dt, doc_publish_timestamp, notice_content_href, notice_id_from_doc
from src.web.industry_insights_service import IndustryInsightsService, get_industry_insights_service


class HeatmapMetric(str, Enum):
    COUNT = "count"
    BUDGET = "budget"
    SCORE = "score"
    COMPANIES = "companies"


class HeatmapLevel(str, Enum):
    PROVINCE = "province"
    CITY = "city"


@dataclass
class HeatmapRegionRow:
    region_code: str
    name: str
    value: float
    pct: float
    company_count: int


class IndustryHeatmapService:
    """按细分行业码 + 空间层级聚合 BidNotice 文档。"""

    def __init__(self, insights: Optional[IndustryInsightsService] = None) -> None:
        self._insights = insights or get_industry_insights_service()

    def heatmap(
        self,
        industry_code: str,
        *,
        metric: HeatmapMetric = HeatmapMetric.COUNT,
        level: HeatmapLevel = HeatmapLevel.PROVINCE,
        days: int = 30,
        domain: str = "数字农业",
        region: str = "",
    ) -> dict[str, Any]:
        code = parse_industry_code(industry_code)
        docs = filter_docs_by_code(self._insights.load_domain_docs(domain, days), code)

        if level == HeatmapLevel.CITY:
            if not region:
                raise ValueError("level=city 需要 region 参数（省级 CN-XX）")
            docs = [d for d in docs if self._doc_region_code(d) == region]
            rows = self._aggregate_by_city(docs, metric, parent_province_code=region)
            coverage_key = "coverage_cities"
        else:
            if region:
                docs = [d for d in docs if self._doc_region_code(d) == region]
            rows = self._aggregate_by_province(docs, metric)
            coverage_key = "coverage_provinces"

        total_value = sum(r.value for r in rows)
        top_purchasers = self.top_purchasers(
            industry_code,
            days=days,
            domain=domain,
            region=region,
            limit=10,
        )

        result: dict[str, Any] = {
            "industry_code": code.raw,
            "industry_name": industry_display_name(code),
            "industry_type": code.type,
            "metric": metric.value,
            "level": level.value,
            "domain": domain,
            "days": days,
            "parent_region": region or None,
            "parent_region_name": province_name_by_code(region) if region else None,
            "total_value": total_value,
            coverage_key: len([r for r in rows if r.value > 0]),
            "regions": [
                {
                    "region_code": r.region_code,
                    "name": r.name,
                    "value": r.value,
                    "pct": r.pct,
                    "company_count": r.company_count,
                }
                for r in rows
            ],
            "geojson": self._geojson_for_level(level, region),
            "top_purchasers": top_purchasers,
            "analyzed_at": app_now().isoformat(),
        }
        return result

    def top_purchasers(
        self,
        industry_code: str,
        *,
        days: int = 30,
        domain: str = "数字农业",
        region: str = "",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        code = parse_industry_code(industry_code)
        docs = filter_docs_by_code(self._insights.load_domain_docs(domain, days), code)
        if region:
            if "::" in region:
                docs = [d for d in docs if self._doc_city_code(d) == region]
            else:
                docs = [d for d in docs if self._doc_region_code(d) == region]

        counter: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "amount": 0.0})
        for doc in docs:
            structured = structured_fields_from_doc(doc, use_llm=False)
            purchaser = (structured.get("purchaser") or structured.get("tender_party") or "").strip()
            if not purchaser:
                continue
            bucket = counter[purchaser]
            bucket["name"] = purchaser
            bucket["count"] += 1
            amount = structured.get("amount_yuan") or parse_amount_yuan(
                structured.get("budget_amount") or structured.get("amount_display")
            )
            bucket["amount"] += float(amount or 0)

        return sorted(counter.values(), key=lambda x: -x["count"])[:limit]

    def region_notices(
        self,
        industry_code: str,
        region_code: str,
        *,
        days: int = 30,
        domain: str = "数字农业",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        code = parse_industry_code(industry_code)
        docs = filter_docs_by_code(self._insights.load_domain_docs(domain, days), code)
        if "::" in region_code:
            matched = [d for d in docs if self._doc_city_code(d) == region_code]
        else:
            matched = [d for d in docs if self._doc_region_code(d) == region_code]
        matched.sort(
            key=lambda d: doc_publish_timestamp(d) or app_now().replace(tzinfo=None),
            reverse=True,
        )
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        start = (page - 1) * page_size
        page_docs = matched[start : start + page_size]
        items = []
        for doc in page_docs:
            structured = structured_fields_from_doc(doc, use_llm=False)
            notice_id = notice_id_from_doc(doc)
            pub = doc_publish_timestamp(doc)
            items.append(
                {
                    "id": notice_id,
                    "content_href": notice_content_href(notice_id),
                    "title": doc.get("title") or "",
                    "url": resolve_notice_detail_url(
                        doc.get("url") or doc.get("detail_url") or "",
                        base_url=doc.get("source_url") or "",
                    ),
                    "purchaser": structured.get("purchaser") or structured.get("tender_party"),
                    "budget_amount": structured.get("budget_amount") or structured.get("amount_display"),
                    "publish_date_fmt": _format_dt(pub) if pub else "—",
                    "region": structured.get("project_location") or doc.get("region"),
                }
            )
        return {
            "industry_code": code.raw,
            "region_code": region_code,
            "region_name": region_name_by_code(region_code) or region_code,
            "page": page,
            "page_size": page_size,
            "total": len(matched),
            "items": items,
        }

    def _doc_region_code(self, doc: dict[str, Any]) -> str | None:
        structured = structured_fields_from_doc(doc, use_llm=False)
        norm = normalize_region_to_province(
            structured.get("project_location") or doc.get("region")
        )
        return norm[0] if norm else None

    def _doc_city_code(self, doc: dict[str, Any]) -> str | None:
        structured = structured_fields_from_doc(doc, use_llm=False)
        location = structured.get("project_location") or doc.get("region")
        prov = self._doc_region_code(doc)
        norm = normalize_region_to_city(location, parent_province_code=prov)
        return norm[0] if norm else None

    def _geojson_for_level(self, level: HeatmapLevel, region: str) -> dict[str, Any]:
        if level == HeatmapLevel.PROVINCE:
            return self._insights.load_geojson()
        return {"type": "FeatureCollection", "features": []}

    def _aggregate_by_city(
        self,
        docs: list[dict[str, Any]],
        metric: HeatmapMetric,
        *,
        parent_province_code: str,
    ) -> list[HeatmapRegionRow]:
        buckets: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"value": 0.0, "companies": set(), "name": ""}
        )
        for doc in docs:
            structured = structured_fields_from_doc(doc, use_llm=False)
            location = structured.get("project_location") or doc.get("region")
            norm = normalize_region_to_city(location, parent_province_code=parent_province_code)
            if not norm:
                continue
            code, name = norm
            buckets[code]["name"] = name
            if metric == HeatmapMetric.COMPANIES:
                purchaser = (structured.get("purchaser") or structured.get("tender_party") or "").strip()
                if purchaser:
                    buckets[code]["companies"].add(purchaser)
                buckets[code]["value"] = float(len(buckets[code]["companies"]))
            else:
                buckets[code]["value"] += self._metric_value(doc, metric)
                purchaser = (structured.get("purchaser") or structured.get("tender_party") or "").strip()
                if purchaser:
                    buckets[code]["companies"].add(purchaser)

        total = sum(b["value"] for b in buckets.values()) or 1.0
        return [
            HeatmapRegionRow(
                region_code=code,
                name=meta["name"],
                value=meta["value"],
                pct=round(meta["value"] / total * 100, 1),
                company_count=len(meta["companies"]),
            )
            for code, meta in sorted(buckets.items(), key=lambda x: -x[1]["value"])
        ]

    def _aggregate_by_province(
        self,
        docs: list[dict[str, Any]],
        metric: HeatmapMetric,
    ) -> list[HeatmapRegionRow]:
        buckets: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"value": 0.0, "companies": set(), "name": ""}
        )
        for doc in docs:
            structured = structured_fields_from_doc(doc, use_llm=False)
            norm = normalize_region_to_province(
                structured.get("project_location") or doc.get("region")
            )
            if not norm:
                continue
            code, name = norm
            buckets[code]["name"] = name
            if metric == HeatmapMetric.COMPANIES:
                purchaser = (structured.get("purchaser") or structured.get("tender_party") or "").strip()
                if purchaser:
                    buckets[code]["companies"].add(purchaser)
                buckets[code]["value"] = float(len(buckets[code]["companies"]))
            else:
                buckets[code]["value"] += self._metric_value(doc, metric)
                purchaser = (structured.get("purchaser") or structured.get("tender_party") or "").strip()
                if purchaser:
                    buckets[code]["companies"].add(purchaser)

        total = sum(b["value"] for b in buckets.values()) or 1.0
        return [
            HeatmapRegionRow(
                region_code=code,
                name=meta["name"],
                value=meta["value"],
                pct=round(meta["value"] / total * 100, 1),
                company_count=len(meta["companies"]),
            )
            for code, meta in sorted(buckets.items(), key=lambda x: -x[1]["value"])
        ]

    def _metric_value(self, doc: dict[str, Any], metric: HeatmapMetric) -> float:
        if metric == HeatmapMetric.COUNT:
            return 1.0
        structured = structured_fields_from_doc(doc, use_llm=False)
        if metric == HeatmapMetric.BUDGET:
            amount = structured.get("amount_yuan") or parse_amount_yuan(
                structured.get("budget_amount") or structured.get("amount_display")
            )
            return float(amount or 0)
        if metric == HeatmapMetric.SCORE:
            return float(score_agri_opportunity(doc).get("score") or 0)
        return 0.0


_service: Optional[IndustryHeatmapService] = None


def get_industry_heatmap_service() -> IndustryHeatmapService:
    global _service
    if _service is None:
        _service = IndustryHeatmapService()
    return _service
