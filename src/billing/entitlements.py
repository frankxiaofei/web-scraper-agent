"""Entitlement 检查与配额 enforcement（Commercial C1）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from src.billing.models import Tenant
from src.billing.plans import get_entitlement_limit, get_plan
from src.billing.usage_service import UsageService, current_period_key, period_key_for_metric


@dataclass
class EntitlementResult:
    allowed: bool
    used: float
    limit: int | None
    plan_id: str
    key: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "used": self.used,
            "limit": self.limit,
            "plan_id": self.plan_id,
            "entitlement": self.key,
        }


_FEATURE_KEY_MAP = {
    "feature.industry_heatmap": "industry_heatmap",
    "feature.supply_chain": "supply_chain",
    "feature.policy_bid_lag": "policy_bid_lag",
    "feature.sop_wizard": "sop_wizard",
    "feature.neo4j_graph": "neo4j_graph",
}

_QUOTA_METRIC_MAP = {
    "quota.crawl_runs": "crawl_runs",
    "quota.llm_tokens": "llm_extraction_tokens",
    "quota.hermes_messages": "hermes_messages",
    "quota.gov_bid_api": "gov_bid_api_calls",
    "quota.storage_gb": "storage_bytes",
    "quota.enabled_sites": "enabled_sites",
}


class EntitlementService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.usage = UsageService(session)

    def get_plan_id(self, tenant_id: UUID) -> str:
        tenant = self.session.get(Tenant, tenant_id)
        return tenant.plan_id if tenant else "free"

    def check(self, tenant_id: UUID, key: str, delta: int = 1) -> EntitlementResult:
        plan_id = self.get_plan_id(tenant_id)
        plan = get_plan(plan_id) or get_plan("free") or {}
        entitlements = plan.get("entitlements") or {}

        if key.startswith("feature."):
            feature = _FEATURE_KEY_MAP.get(key, key.replace("feature.", ""))
            features = list(entitlements.get("features") or [])
            allowed = feature in features
            return EntitlementResult(
                allowed=allowed,
                used=0.0,
                limit=None,
                plan_id=plan_id,
                key=key,
            )

        metric = _QUOTA_METRIC_MAP.get(key, key.replace("quota.", ""))
        period = period_key_for_metric(metric)
        if metric == "enabled_sites":
            from src.billing.site_quota import count_enabled_sites

            used = float(count_enabled_sites())
            limit = entitlements.get("enabled_sites")
            if limit is not None and int(limit) >= 0:
                limit_val = int(limit)
                allowed = (used + delta) <= limit_val
                return EntitlementResult(
                    allowed=allowed,
                    used=used,
                    limit=limit_val,
                    plan_id=plan_id,
                    key=key,
                )
            return EntitlementResult(allowed=True, used=used, limit=None, plan_id=plan_id, key=key)

        used = float(self.usage.sum_metric(tenant_id, metric, period))
        if metric == "storage_bytes":
            used = used / float(1024**3)
        limit = get_entitlement_limit(plan_id, metric if metric != "storage_bytes" else "storage_gb")
        if limit is None:
            allowed = True
        else:
            allowed = (used + delta) <= float(limit)
        return EntitlementResult(
            allowed=allowed,
            used=used,
            limit=limit,
            plan_id=plan_id,
            key=key,
        )

    def consume(self, tenant_id: UUID, key: str, delta: int = 1) -> EntitlementResult:
        result = self.check(tenant_id, key, delta=delta)
        if not result.allowed:
            return result
        metric = _QUOTA_METRIC_MAP.get(key)
        if metric and metric != "enabled_sites":
            self.usage.record(
                tenant_id=tenant_id,
                metric=metric,
                quantity=delta,
                period_key=period_key_for_metric(metric),
                source="entitlement_consume",
            )
        return result


def is_tenant_readonly(tenant_id: UUID) -> bool:
    """订阅 past_due 时进入只读模式（C2-08）。"""
    from src.billing.session import billing_database_configured, get_billing_session
    from src.billing.subscription_service import get_active_subscription

    if not billing_database_configured():
        return False
    session = get_billing_session()
    try:
        sub = get_active_subscription(session, tenant_id)
        if sub is None:
            return False
        return sub.status == "past_due"
    finally:
        session.close()
