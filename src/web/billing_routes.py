"""Billing API routes — plans, usage, Stripe checkout & invoices."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import func, select

from src.billing.invoice_pdf import render_invoice_pdf
from src.billing.models import Invoice, Tenant, User
from src.billing.plans import list_plans
from src.billing.schemas import (
    BuyerInfoRequest,
    CancelSubscriptionResponse,
    CheckoutSessionRequest,
    CheckoutSessionResponse,
    InvoiceListResponse,
    InvoiceSummary,
    PlansListResponse,
    PortalSessionResponse,
    SubscribeRequest,
    SubscriptionResponse,
    UsageSummaryResponse,
)
from src.billing.session import billing_database_configured, get_billing_session
from src.billing.stripe_client import StripeClient, StripeNotConfiguredError, ensure_stripe_customer
from src.billing.subscription_service import (
    SubscriptionNotFoundError,
    UnknownPlanError,
    cancel_subscription,
    get_subscription_summary,
    subscribe_tenant,
)
from src.billing.usage_service import UsageService
from src.billing.webhook_handlers import handle_stripe_event
from src.core.config import get_settings
from src.web.auth.deps import AuthContext, require_auth, require_role

router = APIRouter(prefix="/api/billing", tags=["billing"])

@router.get("/plans", response_model=PlansListResponse)
def billing_plans() -> PlansListResponse:
    return list_plans()


@router.get("/subscription", response_model=SubscriptionResponse)
def billing_subscription(
    auth: AuthContext = Depends(require_auth),
) -> SubscriptionResponse:
    if not billing_database_configured():
        raise HTTPException(status_code=503, detail="DATABASE_URL required for billing subscription")
    session = get_billing_session()
    try:
        summary = get_subscription_summary(session, auth.tenant_id)
        return SubscriptionResponse(**summary)
    finally:
        session.close()


@router.post("/subscribe", response_model=SubscriptionResponse)
def billing_subscribe(
    body: SubscribeRequest,
    auth: AuthContext = Depends(require_role("admin")),
) -> SubscriptionResponse:
    if not billing_database_configured():
        raise HTTPException(status_code=503, detail="DATABASE_URL required for billing subscribe")
    session = get_billing_session()
    try:
        try:
            sub = subscribe_tenant(
                session,
                auth.tenant_id,
                body.plan_id,
                billing_cycle=body.billing_cycle,
            )
        except UnknownPlanError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SubscriptionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return SubscriptionResponse(
            tenant_id=auth.tenant_id,
            plan_id=sub.plan_id,
            status=sub.status,
            billing_cycle=sub.billing_cycle,
            current_period_start=sub.current_period_start.isoformat(),
            current_period_end=sub.current_period_end.isoformat(),
            cancel_at_period_end=sub.cancel_at_period_end,
        )
    finally:
        session.close()


@router.post("/cancel", response_model=CancelSubscriptionResponse)
def billing_cancel(
    auth: AuthContext = Depends(require_role("admin")),
) -> CancelSubscriptionResponse:
    if not billing_database_configured():
        raise HTTPException(status_code=503, detail="DATABASE_URL required for billing cancel")
    session = get_billing_session()
    try:
        try:
            sub = cancel_subscription(session, auth.tenant_id)
        except SubscriptionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return CancelSubscriptionResponse(
            cancel_at_period_end=sub.cancel_at_period_end,
            current_period_end=sub.current_period_end.isoformat(),
        )
    finally:
        session.close()


@router.post("/checkout-session", response_model=CheckoutSessionResponse)
def billing_checkout_session(
    body: CheckoutSessionRequest,
    auth: AuthContext = Depends(require_role("admin")),
) -> CheckoutSessionResponse:
    if not billing_database_configured():
        raise HTTPException(status_code=503, detail="DATABASE_URL required")
    settings = get_settings()
    price_id = settings.stripe_price_pro_monthly
    if not price_id:
        raise HTTPException(status_code=503, detail="STRIPE_PRICE_PRO_MONTHLY not configured")
    session = get_billing_session()
    try:
        tenant = session.get(Tenant, auth.tenant_id)
        user = session.get(User, auth.user_id)
        if tenant is None or user is None:
            raise HTTPException(status_code=404, detail="tenant or user not found")
        try:
            customer_id = ensure_stripe_customer(
                session,
                auth.tenant_id,
                email=user.email,
                name=tenant.name,
            )
            checkout = StripeClient().create_checkout_session(
                customer_id=customer_id,
                price_id=price_id,
                success_url=body.success_url,
                cancel_url=body.cancel_url,
                metadata={
                    "tenant_id": str(auth.tenant_id),
                    "plan_id": body.plan_id,
                    "billing_cycle": body.billing_cycle,
                },
            )
        except StripeNotConfiguredError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return CheckoutSessionResponse(checkout_url=checkout["url"] or "")
    finally:
        session.close()


@router.get("/portal", response_model=PortalSessionResponse)
def billing_portal(
    auth: AuthContext = Depends(require_role("admin")),
    return_url: str = Query("/settings/billing"),
) -> PortalSessionResponse:
    if not billing_database_configured():
        raise HTTPException(status_code=503, detail="DATABASE_URL required")
    session = get_billing_session()
    try:
        tenant = session.get(Tenant, auth.tenant_id)
        user = session.get(User, auth.user_id)
        if tenant is None or user is None:
            raise HTTPException(status_code=404, detail="tenant or user not found")
        customer_id = tenant.stripe_customer_id
        if not customer_id:
            customer_id = ensure_stripe_customer(
                session, auth.tenant_id, email=user.email, name=tenant.name
            )
        try:
            portal = StripeClient().create_portal_session(
                customer_id=customer_id,
                return_url=return_url,
            )
        except StripeNotConfiguredError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return PortalSessionResponse(portal_url=portal["url"] or "")
    finally:
        session.close()


@router.post("/webhook")
async def billing_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = StripeClient().construct_webhook_event(payload, sig)
    except StripeNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid webhook: {exc}") from exc
    if not billing_database_configured():
        raise HTTPException(status_code=503, detail="DATABASE_URL required")
    session = get_billing_session()
    try:
        return handle_stripe_event(session, event)
    finally:
        session.close()


@router.get("/invoices", response_model=InvoiceListResponse)
def billing_invoices(
    auth: AuthContext = Depends(require_role("admin")),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> InvoiceListResponse:
    if not billing_database_configured():
        raise HTTPException(status_code=503, detail="DATABASE_URL required")
    session = get_billing_session()
    try:
        total = session.scalar(
            select(func.count()).select_from(Invoice).where(Invoice.tenant_id == auth.tenant_id)
        ) or 0
        rows = session.scalars(
            select(Invoice)
            .where(Invoice.tenant_id == auth.tenant_id)
            .order_by(Invoice.created_at.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        invoices = [
            InvoiceSummary(
                id=inv.id,
                number=inv.number,
                status=inv.status,
                amount_cny=inv.amount_cny,
                currency=inv.currency,
                period_start=str(inv.period_start),
                period_end=str(inv.period_end),
                paid_at=inv.paid_at.isoformat() if inv.paid_at else None,
                pdf_url=f"/api/billing/invoices/{inv.id}/pdf",
            )
            for inv in rows
        ]
        return InvoiceListResponse(invoices=invoices, total=int(total))
    finally:
        session.close()


@router.get("/invoices/{invoice_id}/pdf")
def billing_invoice_pdf(
    invoice_id: UUID,
    auth: AuthContext = Depends(require_role("admin")),
):
    if not billing_database_configured():
        raise HTTPException(status_code=503, detail="DATABASE_URL required")
    session = get_billing_session()
    try:
        inv = session.get(Invoice, invoice_id)
        if inv is None or inv.tenant_id != auth.tenant_id:
            raise HTTPException(status_code=404, detail="invoice not found")
        tenant = session.get(Tenant, auth.tenant_id)
        pdf_bytes = render_invoice_pdf(inv, tenant)
        return Response(content=pdf_bytes, media_type="application/pdf")
    finally:
        session.close()


@router.post("/buyer-info")
def billing_buyer_info(
    body: BuyerInfoRequest,
    auth: AuthContext = Depends(require_role("admin")),
):
    if not billing_database_configured():
        raise HTTPException(status_code=503, detail="DATABASE_URL required")
    session = get_billing_session()
    try:
        tenant = session.get(Tenant, auth.tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail="tenant not found")
        tenant.tax_id = body.buyer_tax_id
        settings = dict(tenant.settings or {})
        settings["buyer_name"] = body.buyer_name
        tenant.settings = settings
        session.commit()
        return {"ok": True, "buyer_name": body.buyer_name, "buyer_tax_id": body.buyer_tax_id}
    finally:
        session.close()


@router.get("/usage", response_model=UsageSummaryResponse)
def billing_usage(
    auth: AuthContext = Depends(require_auth),
    period: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
) -> UsageSummaryResponse:
    if not billing_database_configured():
        raise HTTPException(status_code=503, detail="DATABASE_URL required for billing usage")
    session = get_billing_session()
    try:
        return UsageService(session).get_usage_summary(auth.tenant_id, period_key=period)
    finally:
        session.close()
