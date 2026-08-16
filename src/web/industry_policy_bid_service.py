"""政策-招标时滞关联洞察（Phase 0 MVP）。"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional

from src.core.notice_field_utils import parse_amount_yuan, structured_fields_from_doc
from src.core.policy_news_fetcher import get_policy_news_fetcher
from src.core.timezone_utils import app_now
from src.industry.domains_loader import get_domain_config
from src.web.industry_insights_service import IndustryInsightsService, get_industry_insights_service

logger = logging.getLogger(__name__)


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _doc_publish(doc: dict[str, Any]) -> datetime | None:
    from src.web.data_service import doc_publish_timestamp

    return doc_publish_timestamp(doc)


def _doc_budget(doc: dict[str, Any]) -> float:
    structured = structured_fields_from_doc(doc, use_llm=False)
    amount = structured.get("amount_yuan") or parse_amount_yuan(
        structured.get("budget_amount") or structured.get("amount_display")
    )
    return float(amount or 0)


def _doc_matches_theme(doc: dict[str, Any], theme: str, domain_keywords: list[str]) -> bool:
    haystack = "\n".join(
        str(doc.get(k) or "") for k in ("title", "content_text", "key_summary", "category")
    )
    if theme and theme in haystack:
        return True
    return any(kw in haystack for kw in domain_keywords)


class IndustryPolicyBidService:
    """政策发布后 N 天内同主题招标量/预算变化（运行时聚合）。"""

    def __init__(self, insights: Optional[IndustryInsightsService] = None) -> None:
        self._insights = insights or get_industry_insights_service()

    def _load_policies(self, days: int) -> list[dict[str, Any]]:
        return (get_policy_news_fetcher().list_news(limit=200, days=days).get("items") or [])

    def policy_bid_lag(
        self,
        domain: str = "数字农业",
        lag_days: int = 90,
        *,
        days: int = 180,
    ) -> dict[str, Any]:
        cfg = get_domain_config(domain) or {}
        domain_keywords = list(cfg.get("keywords") or [])
        policies = self._load_policies(days)
        bid_docs = self._insights.load_domain_docs(domain, days)

        theme_policies: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in policies:
            themes = item.get("themes") or ["其他"]
            for theme in themes:
                theme_policies[str(theme)].append(item)

        themes_out: list[dict[str, Any]] = []
        for theme, plist in theme_policies.items():
            policy_count = len(plist)
            bid_count_after = 0
            bid_budget_after = 0.0
            baseline_count = 0
            baseline_budget = 0.0

            for policy in plist:
                pub = _parse_dt(policy.get("published_at"))
                if not pub:
                    continue
                after_start = pub
                after_end = pub + timedelta(days=lag_days)
                before_start = pub - timedelta(days=lag_days)
                before_end = pub

                for doc in bid_docs:
                    doc_pub = _doc_publish(doc)
                    if not doc_pub or not _doc_matches_theme(doc, theme, domain_keywords):
                        continue
                    if after_start <= doc_pub <= after_end:
                        bid_count_after += 1
                        bid_budget_after += _doc_budget(doc)
                    elif before_start <= doc_pub < before_end:
                        baseline_count += 1
                        baseline_budget += _doc_budget(doc)

            lift_pct = 0.0
            if baseline_count > 0:
                lift_pct = round((bid_count_after - baseline_count) / baseline_count * 100, 1)
            elif bid_count_after > 0:
                lift_pct = 100.0

            themes_out.append(
                {
                    "theme": theme,
                    "policy_count": policy_count,
                    "bid_count_after": bid_count_after,
                    "bid_budget_after": round(bid_budget_after, 2),
                    "baseline_bid_count": baseline_count,
                    "lift_pct": lift_pct,
                    "signal": "重点跟进" if lift_pct >= 50 else "观望",
                }
            )

        themes_out.sort(key=lambda x: -x["lift_pct"])
        return {
            "domain": domain,
            "lag_days": lag_days,
            "days": days,
            "themes": themes_out,
            "analyzed_at": app_now().isoformat(),
        }

    def policy_bid_trend(
        self,
        theme: str,
        *,
        days: int = 180,
        bucket: str = "week",
        domain: str = "数字农业",
    ) -> dict[str, Any]:
        if bucket != "week":
            raise ValueError("Phase 0 仅支持 bucket=week")

        cfg = get_domain_config(domain) or {}
        domain_keywords = list(cfg.get("keywords") or [])
        policies = self._load_policies(days)
        bid_docs = self._insights.load_domain_docs(domain, days)
        cutoff = datetime.now() - timedelta(days=days)

        buckets: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"policy_count": 0, "bid_count": 0, "bid_budget": 0.0}
        )

        for item in policies:
            pub = _parse_dt(item.get("published_at"))
            if not pub or pub < cutoff:
                continue
            themes = [str(t) for t in (item.get("themes") or [])]
            if theme not in themes and theme not in (item.get("title") or ""):
                continue
            week = pub.strftime("%Y-W%W")
            buckets[week]["policy_count"] += 1

        for doc in bid_docs:
            pub = _doc_publish(doc)
            if not pub or pub < cutoff:
                continue
            if not _doc_matches_theme(doc, theme, domain_keywords):
                continue
            week = pub.strftime("%Y-W%W")
            buckets[week]["bid_count"] += 1
            buckets[week]["bid_budget"] += _doc_budget(doc)

        series = [
            {"week": week, **data}
            for week, data in sorted(buckets.items())
        ]
        return {
            "theme": theme,
            "domain": domain,
            "days": days,
            "bucket": bucket,
            "series": series,
            "analyzed_at": app_now().isoformat(),
        }


_service: Optional[IndustryPolicyBidService] = None


def get_industry_policy_bid_service() -> IndustryPolicyBidService:
    global _service
    if _service is None:
        _service = IndustryPolicyBidService()
    return _service
