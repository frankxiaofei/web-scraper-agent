"""Quota enforcement helpers (Commercial C1-09~13)."""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from src.billing.entitlements import EntitlementResult, EntitlementService
from src.billing.session import billing_database_configured, billing_session_scope
from src.core.config import get_settings
from src.billing.site_quota import count_enabled_sites


def _billing_active() -> bool:
    return bool(get_settings().billing_enabled and billing_database_configured())


def check_entitlement(
    tenant_id: UUID | None,
    key: str,
    *,
    delta: int = 1,
    enabled_sites_count: int | None = None,
) -> EntitlementResult:
    """Check entitlement; always allowed when billing disabled."""
    if not _billing_active() or tenant_id is None:
        return EntitlementResult(
            allowed=True,
            used=0.0,
            limit=None,
            plan_id="free",
            key=key,
        )

    with billing_session_scope() as session:
        svc = EntitlementService(session)
        if key == "quota.enabled_sites" and enabled_sites_count is not None:
            plan_id = svc.get_plan_id(tenant_id)
            from src.billing.plans import get_plan

            plan = get_plan(plan_id) or get_plan("free") or {}
            limit = (plan.get("entitlements") or {}).get("enabled_sites")
            used = float(enabled_sites_count)
            if limit is None or int(limit) < 0:
                return EntitlementResult(
                    allowed=True, used=used, limit=None, plan_id=plan_id, key=key
                )
            limit_val = int(limit)
            return EntitlementResult(
                allowed=(used + delta) <= limit_val,
                used=used,
                limit=limit_val,
                plan_id=plan_id,
                key=key,
            )
        return svc.check(tenant_id, key, delta=delta)


def enforce_entitlement(
    tenant_id: UUID | None,
    key: str,
    *,
    delta: int = 1,
    enabled_sites_count: int | None = None,
) -> EntitlementResult:
    """Raise HTTP 403 quota_exceeded when not allowed."""
    result = check_entitlement(
        tenant_id,
        key,
        delta=delta,
        enabled_sites_count=enabled_sites_count,
    )
    if not result.allowed:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "quota_exceeded",
                "entitlement": key,
                "used": result.used,
                "limit": result.limit,
                "plan_id": result.plan_id,
            },
        )
    return result


def consume_entitlement(
    tenant_id: UUID | None,
    key: str,
    *,
    delta: int = 1,
) -> EntitlementResult:
    """Check + record usage for metered quotas."""
    if not _billing_active() or tenant_id is None:
        return EntitlementResult(
            allowed=True, used=0.0, limit=None, plan_id="free", key=key
        )
    with billing_session_scope() as session:
        svc = EntitlementService(session)
        result = svc.consume(tenant_id, key, delta=delta)
        if not result.allowed:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "quota_exceeded",
                    "entitlement": key,
                    "used": result.used,
                    "limit": result.limit,
                    "plan_id": result.plan_id,
                },
            )
        return result


def tenant_from_request(request: Any) -> UUID | None:
    tid = getattr(getattr(request, "state", None), "tenant_id", None)
    if isinstance(tid, UUID):
        return tid
    if tid:
        try:
            return uuid.UUID(str(tid))
        except ValueError:
            return None
    return None
