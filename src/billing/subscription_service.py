"""订阅生命周期管理（Commercial C1）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.billing.models import Subscription, Tenant
from src.billing.plans import get_plan


class UnknownPlanError(Exception):
    pass


class SubscriptionNotFoundError(Exception):
    pass


def _period_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    start = now or datetime.now(timezone.utc)
    end = start + timedelta(days=30)
    return start, end


def get_active_subscription(session: Session, tenant_id: UUID) -> Subscription | None:
    return session.scalar(
        select(Subscription)
        .where(
            Subscription.tenant_id == tenant_id,
            Subscription.status == "active",
        )
        .order_by(Subscription.created_at.desc())
    )


def get_subscription_summary(session: Session, tenant_id: UUID) -> dict:
    tenant = session.get(Tenant, tenant_id)
    plan_id = tenant.plan_id if tenant else "free"
    sub = get_active_subscription(session, tenant_id)
    if sub is None:
        start, end = _period_bounds()
        return {
            "tenant_id": tenant_id,
            "plan_id": plan_id,
            "status": "active",
            "billing_cycle": "monthly",
            "current_period_start": start.isoformat(),
            "current_period_end": end.isoformat(),
            "cancel_at_period_end": False,
        }
    return {
        "tenant_id": tenant_id,
        "plan_id": sub.plan_id,
        "status": sub.status,
        "billing_cycle": sub.billing_cycle,
        "current_period_start": sub.current_period_start.isoformat(),
        "current_period_end": sub.current_period_end.isoformat(),
        "cancel_at_period_end": sub.cancel_at_period_end,
    }


def subscribe_tenant(
    session: Session,
    tenant_id: UUID,
    plan_id: str,
    *,
    billing_cycle: str = "monthly",
) -> Subscription:
    if get_plan(plan_id) is None:
        raise UnknownPlanError(f"unknown plan: {plan_id}")

    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise SubscriptionNotFoundError("tenant not found")

    start, end = _period_bounds()
    existing = get_active_subscription(session, tenant_id)
    if existing:
        existing.plan_id = plan_id
        existing.status = "active"
        existing.billing_cycle = billing_cycle
        existing.current_period_start = start
        existing.current_period_end = end
        existing.cancel_at_period_end = False
        existing.canceled_at = None
        sub = existing
    else:
        sub = Subscription(
            tenant_id=tenant_id,
            plan_id=plan_id,
            status="active",
            billing_cycle=billing_cycle,
            payment_provider="manual",
            current_period_start=start,
            current_period_end=end,
        )
        session.add(sub)

    tenant.plan_id = plan_id
    session.commit()
    session.refresh(sub)
    return sub


def cancel_subscription(session: Session, tenant_id: UUID) -> Subscription:
    sub = get_active_subscription(session, tenant_id)
    if sub is None:
        raise SubscriptionNotFoundError("no active subscription")
    sub.cancel_at_period_end = True
    session.commit()
    session.refresh(sub)
    return sub
