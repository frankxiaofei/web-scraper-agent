"""行业洞察聚合服务（Phase 0 MVP）。"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from src.core.agri_classifier import doc_is_agri_related, score_agri_opportunity
from src.core.agri_sites import get_insights_tender_site_ids
from src.core.biz_clue_config import load_biz_clue_keywords
from src.core.notice_field_utils import parse_amount_yuan, structured_fields_from_doc
from src.core.timezone_utils import app_now
from src.core.url_utils import resolve_notice_detail_url
from src.industry.gb2017_taxonomy import build_gb2017_cascader, flatten_gb2017_items
from src.industry.domains_loader import default_domain, get_domain_config, list_domain_names
from src.industry.pseudo_industry import (
    PseudoIndustryCode,
    filter_docs_by_code,
    industry_display_name,
    parse_industry_code,
)
from src.industry.region_normalizer import normalize_region_to_province
from src.web.biz_clue_service import match_product_lines
from src.web.data_service import (
    NOTICE_MONGO_SORT,
    NoticeDataService,
    doc_publish_timestamp,
    get_data_service,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
INSIGHTS_CACHE_PATH = ROOT / "data" / "industry_insights_cache.json"
GEOJSON_PATH = ROOT / "src" / "web" / "static" / "geo" / "cn_provinces.json"
DEFAULT_DAYS = 30
MAX_DAYS = 90
_CACHE_TTL_SECONDS = 60.0
_DOCS_PROJECTION = {"content_html": 0, "content_text": 0, "attachments": 0}

_service: Optional["IndustryInsightsService"] = None
_docs_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_result_cache: dict[str, tuple[float, Any]] = {}


def _match_any_keyword(doc: dict[str, Any], keywords: list[str]) -> bool:
    if not keywords:
        return True
    haystack = "\n".join(
        str(doc.get(k) or "") for k in ("title", "content_text", "key_summary", "category")
    )
    return any(kw in haystack for kw in keywords)


def _doc_timestamp(doc: dict[str, Any]) -> Optional[datetime]:
    return doc_publish_timestamp(doc)


class IndustryInsightsService:
    """行业域公告加载、KPI、分布与采购链聚合。"""

    def __init__(self, data_service: Optional[NoticeDataService] = None) -> None:
        self._data = data_service or get_data_service()

    def load_domain_docs(
        self,
        domain: str,
        days: int = DEFAULT_DAYS,
        *,
        site_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        cfg = get_domain_config(domain)
        if cfg is None:
            raise ValueError(f"unknown domain: {domain}")

        days = max(1, min(days, MAX_DAYS))
        cutoff = datetime.now() - timedelta(days=days)
        site_ids = cfg.get("site_ids") or get_insights_tender_site_ids()
        allowed = set(site_ids)
        if site_id:
            allowed = {site_id} if site_id in allowed else set()

        cache_key = f"docs:{domain}:{days}:{site_id or 'all'}"
        now = time.monotonic()
        cached = _docs_cache.get(cache_key)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]

        docs = self._load_notices_since(cutoff, allowed)
        keywords = cfg.get("keywords") or []
        if keywords:
            docs = [d for d in docs if _match_any_keyword(d, keywords)]
        if cfg.get("agri_only", domain == default_domain()):
            docs = [d for d in docs if doc_is_agri_related(d)]

        _docs_cache[cache_key] = (now, docs)
        return docs

    def _load_notices_since(
        self, cutoff: datetime, allowed_site_ids: set[str]
    ) -> list[dict[str, Any]]:
        if self._data.data_source == "mongodb" and self._data._mongo_coll is not None:
            coll = self._data._mongo_coll
            query: dict[str, Any] = {}
            if allowed_site_ids:
                query["$or"] = [
                    {"source_site_id": {"$in": list(allowed_site_ids)}},
                    {"source": {"$in": list(allowed_site_ids)}},
                    {"site_id": {"$in": list(allowed_site_ids)}},
                ]
            docs: list[dict[str, Any]] = []
            for doc in coll.find(query, _DOCS_PROJECTION).sort(NOTICE_MONGO_SORT):
                sid = doc.get("source_site_id") or doc.get("source") or doc.get("site_id") or ""
                if allowed_site_ids and sid not in allowed_site_ids:
                    continue
                ts = _doc_timestamp(doc)
                if ts is not None and ts < cutoff:
                    break
                docs.append(doc)
            return docs

        docs: list[dict[str, Any]] = []
        for doc in self._data._load_jsonl():  # noqa: SLF001
            sid = doc.get("source_site_id") or doc.get("source") or doc.get("site_id") or ""
            if allowed_site_ids and sid not in allowed_site_ids:
                continue
            ts = _doc_timestamp(doc)
            if ts is not None and ts < cutoff:
                continue
            docs.append(doc)
        docs.sort(key=lambda d: _doc_timestamp(d) or datetime.min, reverse=True)
        return docs

    def _cache_get(self, key: str) -> Any | None:
        now = time.monotonic()
        cached = _result_cache.get(key)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]
        return None

    def _cache_set(self, key: str, value: Any) -> None:
        _result_cache[key] = (time.monotonic(), value)

    def summary(
        self,
        *,
        domain: str = "数字农业",
        days: int = DEFAULT_DAYS,
    ) -> dict[str, Any]:
        cache_key = f"summary:{domain}:{days}"
        hit = self._cache_get(cache_key)
        if hit is not None:
            return hit

        docs = self.load_domain_docs(domain, days)
        companies: set[str] = set()
        provinces: set[str] = set()
        edge_keys: set[tuple[str, str]] = set()

        for doc in docs:
            structured = structured_fields_from_doc(doc, use_llm=False)
            purchaser = (structured.get("purchaser") or structured.get("tender_party") or "").strip()
            supplier = (structured.get("tender_party") or "").strip()
            if purchaser:
                companies.add(purchaser)
            norm = normalize_region_to_province(
                structured.get("project_location") or doc.get("region")
            )
            if norm:
                provinces.add(norm[0])
            if purchaser:
                edge_keys.add((purchaser, supplier or "—"))

        result = {
            "domain": domain,
            "days": days,
            "total_notices": len(docs),
            "total_companies": len(companies),
            "coverage_provinces": len(provinces),
            "supply_chain_edges": len(edge_keys),
            "analyzed_at": app_now().isoformat(),
        }
        self._cache_set(cache_key, result)
        return result

    def distribution(
        self,
        *,
        domain: str = "数字农业",
        days: int = DEFAULT_DAYS,
        level: str = "province",
    ) -> dict[str, Any]:
        if level != "province":
            raise ValueError("Phase 0 仅支持 level=province")

        cache_key = f"distribution:{domain}:{days}:{level}"
        hit = self._cache_get(cache_key)
        if hit is not None:
            return hit

        docs = self.load_domain_docs(domain, days)
        buckets: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"value": 0, "companies": set(), "name": ""}
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
            buckets[code]["value"] += 1
            purchaser = (structured.get("purchaser") or structured.get("tender_party") or "").strip()
            if purchaser:
                buckets[code]["companies"].add(purchaser)

        total = sum(b["value"] for b in buckets.values()) or 1
        regions = [
            {
                "region_code": code,
                "name": meta["name"],
                "value": meta["value"],
                "pct": round(meta["value"] / total * 100, 1),
                "company_count": len(meta["companies"]),
            }
            for code, meta in sorted(buckets.items(), key=lambda x: -x[1]["value"])
        ]
        result = {
            "domain": domain,
            "days": days,
            "level": level,
            "total_value": sum(b["value"] for b in buckets.values()),
            "regions": regions,
            "geojson": self.load_geojson(),
            "analyzed_at": app_now().isoformat(),
        }
        self._cache_set(cache_key, result)
        return result

    def supply_chain_table(
        self,
        industry_code: str,
        *,
        days: int = DEFAULT_DAYS,
        domain: str = "数字农业",
        limit: int = 50,
    ) -> dict[str, Any]:
        cache_key = f"supply:{industry_code}:{domain}:{days}:{limit}"
        hit = self._cache_get(cache_key)
        if hit is not None:
            return hit

        code = parse_industry_code(industry_code)
        docs = filter_docs_by_code(self.load_domain_docs(domain, days), code)
        edge_map: dict[tuple[str, str], dict[str, Any]] = {}

        for doc in docs:
            structured = structured_fields_from_doc(doc, use_llm=False)
            purchaser = (
                structured.get("purchaser")
                or structured.get("agency")
                or structured.get("tender_party")
                or ""
            ).strip()
            supplier = (structured.get("tender_party") or "").strip()
            if purchaser and supplier == purchaser:
                supplier = "—"
            if not purchaser:
                continue
            key = (purchaser, supplier or "—")
            bucket = edge_map.setdefault(
                key,
                {
                    "purchaser": purchaser,
                    "supplier": supplier or "—",
                    "notice_count": 0,
                    "total_amount": 0.0,
                    "latest_date": None,
                    "sample_url": "",
                },
            )
            bucket["notice_count"] += 1
            amount = structured.get("amount_yuan")
            if amount is None:
                amount = parse_amount_yuan(
                    structured.get("budget_amount") or structured.get("amount_display")
                )
            bucket["total_amount"] += float(amount or 0)
            pub = _doc_timestamp(doc)
            pub_iso = pub.isoformat() if pub else None
            if pub_iso and (bucket["latest_date"] is None or pub_iso > bucket["latest_date"]):
                bucket["latest_date"] = pub_iso
                bucket["sample_url"] = resolve_notice_detail_url(
                    doc.get("url") or doc.get("detail_url") or "",
                    base_url=doc.get("source_url") or "",
                )

        edges = sorted(edge_map.values(), key=lambda x: -x["notice_count"])[:limit]
        result = {
            "industry_code": code.raw,
            "industry_name": industry_display_name(code),
            "domain": domain,
            "days": days,
            "edges": edges,
            "analyzed_at": app_now().isoformat(),
        }
        self._cache_set(cache_key, result)
        return result

    def taxonomy(
        self,
        *,
        domain: str = "数字农业",
        taxonomy: str = "PSEUDO",
    ) -> dict[str, Any]:
        cfg = get_domain_config(domain)
        if cfg is None:
            raise ValueError(f"unknown domain: {domain}")

        if taxonomy.upper() == "GB2017":
            tree = build_gb2017_cascader()
            return {
                "domain": domain,
                "taxonomy": "GB2017",
                "tree": tree,
                "groups": [
                    {
                        "id": "gb2017",
                        "label": "GB/T 4754-2017",
                        "items": flatten_gb2017_items(),
                    }
                ],
                "analyzed_at": app_now().isoformat(),
            }

        groups: list[dict[str, Any]] = []
        if taxonomy.upper() == "PSEUDO":
            pl_cfg = load_biz_clue_keywords()
            product_lines = list((pl_cfg.get("product_lines") or {}).keys())
            groups.append(
                {
                    "id": "product_lines",
                    "label": "产品线",
                    "items": [
                        {"code": f"product_line:{name}", "name": name, "type": "product_line"}
                        for name in product_lines
                    ],
                }
            )
            tag_items: list[dict[str, str]] = []
            seen_tags: set[str] = set()
            for doc in self.load_domain_docs(domain, DEFAULT_DAYS)[:200]:
                for match in match_product_lines(doc):
                    pl = match.get("product_line")
                    if pl and pl not in seen_tags:
                        seen_tags.add(pl)
                for kw in doc.get("agri_tags") or []:
                    tag = str(kw)
                    if tag not in seen_tags:
                        seen_tags.add(tag)
                        tag_items.append({"code": f"tag:{tag}", "name": tag, "type": "tag"})
            if not tag_items:
                for tag in ("农业物联网", "智慧农业", "数字农业", "精准农业"):
                    tag_items.append({"code": f"tag:{tag}", "name": tag, "type": "tag"})
            groups.append({"id": "agri_tags", "label": "农业标签", "items": tag_items[:20]})
            groups.append(
                {
                    "id": "domains",
                    "label": "行业域",
                    "items": [
                        {"code": f"domain:{name}", "name": name, "type": "domain"}
                        for name in list_domain_names()
                    ],
                }
            )

        return {
            "domain": domain,
            "taxonomy": taxonomy,
            "groups": groups,
            "analyzed_at": app_now().isoformat(),
        }

    def load_geojson(self) -> dict[str, Any]:
        if GEOJSON_PATH.is_file():
            with GEOJSON_PATH.open(encoding="utf-8") as f:
                return json.load(f)
        return {"type": "FeatureCollection", "features": []}

    def recommend_industries(
        self,
        *,
        domain: str = "数字农业",
        days: int = DEFAULT_DAYS,
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        docs = self.load_domain_docs(domain, days)
        counter: Counter[str] = Counter()
        for doc in docs:
            for match in match_product_lines(doc):
                pl = match.get("product_line")
                if pl:
                    counter[str(pl)] += 1
        return [
            {
                "code": f"product_line:{name}",
                "name": name,
                "count": cnt,
                "type": "product_line",
            }
            for name, cnt in counter.most_common(limit)
        ]


def get_industry_insights_service() -> IndustryInsightsService:
    global _service
    if _service is None:
        _service = IndustryInsightsService()
    return _service
