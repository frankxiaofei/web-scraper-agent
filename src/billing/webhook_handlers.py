"""Stripe / 支付 Webhook 处理（Commercial C2 C2-05~C2-07, C2-12）。"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.billing.models import Invoice, Subscription, Tenant, WebhookEvent
from src.billing.subscription_service import subscribe_tenant

logger = logging.getLogger(__name__)


def _already_processed(session: Session, provider: str, external_id: str) -> bool:
    row = session.scalar(
        select(WebhookEvent.id).where(
            WebhookEvent.provider == provider,
            WebhookEvent.external_id == external_id,
        )
    )
    return row is not None


def _record_webhook(session: Session, provider: str, external_id: str, event_type: str, payload: dict) -> None:
    session.add(
        WebhookEvent(
            provider=provider,
            external_id=external_id,
            event_type=event_type,
            payload=payload,
        )
    )


def handle_stripe_event(session: Session, event: dict[str, Any]) -> dict[str, Any]:
    """幂等分发 Stripe webhook（C2-05 / C2-06）。"""
    event_id = event.get("id") or ""
    event_type = event.get("type") or ""
    if not event_id:
        return {"ok": False, "error": "missing event id"}
    if _already_processed(session, "stripe", event_id):
        return {"ok": True, "duplicate": True, "event_type": event_type}

    data_object = (event.get("data") or {}).get("object") or {}
    result: dict[str, Any] = {"ok": True, "event_type": event_type}

    if event_type == "checkout.session.completed":
        result.update(_handle_checkout_completed(session, data_object))
    elif event_type in {"customer.subscription.updated", "customer.subscription.deleted"}:
        result.update(_handle_subscription_change(session, data_object, deleted=event_type.endswith(".deleted")))
    elif event_type == "invoice.paid":
        result.update(_handle_invoice_paid(session, data_object))
    elif event_type == "invoice.payment_failed":
        result.update(_handle_payment_failed(session, data_object))

    _record_webhook(session, "stripe", event_id, event_type, event)
    session.commit()
    return result


def _tenant_from_metadata(session: Session, metadata: dict[str, Any]) -> Tenant | None:
    tenant_id = metadata.get("tenant_id")
    if not tenant_id:
        return None
    try:
        return session.get(Tenant, UUID(str(tenant_id)))
    except ValueError:
        return None


def _handle_checkout_completed(session: Session, obj: dict[str, Any]) -> dict[str, Any]:
    metadata = obj.get("metadata") or {}
    tenant = _tenant_from_metadata(session, metadata)
    plan_id = metadata.get("plan_id") or "pro"
    if tenant is None:
        return {"handled": False, "reason": "tenant not found"}
    subscribe_tenant(session, tenant.id, plan_id, billing_cycle=metadata.get("billing_cycle") or "monthly")
    sub = session.scalar(
        select(Subscription)
        .where(Subscription.tenant_id == tenant.id)
        .order_by(Subscription.created_at.desc())
    )
    if sub:
        sub.payment_provider = "stripe"
        sub.stripe_subscription_id = obj.get("subscription")
        session.commit()
    return {"handled": True, "tenant_id": str(tenant.id), "plan_id": plan_id}


def _handle_subscription_change(session: Session, obj: dict[str, Any], *, deleted: bool) -> dict[str, Any]:
    stripe_sub_id = obj.get("id")
    if not stripe_sub_id:
        return {"handled": False}
    sub = session.scalar(
        select(Subscription).where(Subscription.stripe_subscription_id == stripe_sub_id)
    )
    if sub is None:
        return {"handled": False, "reason": "subscription not found"}
    if deleted:
        sub.status = "canceled"
        sub.canceled_at = datetime.now(timezone.utc)
    else:
        sub.status = obj.get("status") or sub.status
        period = obj.get("current_period_end")
        if period:
            sub.current_period_end = datetime.fromtimestamp(int(period), tz=timezone.utc)
    session.commit()
    return {"handled": True, "subscription_id": str(sub.id), "status": sub.status}


def _handle_payment_failed(session: Session, obj: dict[str, Any]) -> dict[str, Any]:
    customer_id = obj.get("customer")
    tenant = session.scalar(select(Tenant).where(Tenant.stripe_customer_id == customer_id))
    if tenant is None:
        return {"handled": False}
    sub = session.scalar(
        select(Subscription)
        .where(Subscription.tenant_id == tenant.id)
        .order_by(Subscription.created_at.desc())
    )
    if sub:
        sub.status = "past_due"
        session.commit()
    return {"handled": True, "tenant_id": str(tenant.id), "status": "past_due"}


def _handle_invoice_paid(session: Session, obj: dict[str, Any]) -> dict[str, Any]:
    customer_id = obj.get("customer")
    tenant = session.scalar(select(Tenant).where(Tenant.stripe_customer_id == customer_id))
    if tenant is None:
        return {"handled": False}
    stripe_invoice_id = obj.get("id")
    existing = session.scalar(
        select(Invoice).where(Invoice.stripe_invoice_id == stripe_invoice_id)
    )
    amount = int(obj.get("amount_paid") or obj.get("total") or 0)
    period_start = date.today()
    period_end = date.today()
    lines = obj.get("lines") or {}
    data = (lines.get("data") or [{}])[0]
    period = data.get("period") or {}
    if period.get("start"):
        period_start = datetime.fromtimestamp(int(period["start"]), tz=timezone.utc).date()
    if period.get("end"):
        period_end = datetime.fromtimestamp(int(period["end"]), tz=timezone.utc).date()
    number = obj.get("number") or f"STRIPE-{stripe_invoice_id[-8:]}"
    if existing:
        existing.status = "paid"
        existing.paid_at = datetime.now(timezone.utc)
        existing.amount_cny = max(amount // 100, 0)
        session.commit()
        return {"handled": True, "invoice_id": str(existing.id), "updated": True}
    inv = Invoice(
        tenant_id=tenant.id,
        number=number,
        status="paid",
        amount_cny=max(amount // 100, 0),
        period_start=period_start,
        period_end=period_end,
        payment_provider="stripe",
        stripe_invoice_id=stripe_invoice_id,
        paid_at=datetime.now(timezone.utc),
    )
    session.add(inv)
    session.commit()
    return {"handled": True, "invoice_id": str(inv.id), "created": True}
