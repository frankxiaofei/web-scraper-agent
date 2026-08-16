"""Stripe SDK 封装（Commercial C2 C2-01）。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from src.core.config import Settings, get_settings


class StripeNotConfiguredError(Exception):
    pass


class StripeClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._stripe = None

    @property
    def configured(self) -> bool:
        return bool(self._settings.stripe_secret_key)

    def _client(self):
        if not self.configured:
            raise StripeNotConfiguredError("STRIPE_SECRET_KEY not configured")
        if self._stripe is None:
            import stripe

            stripe.api_key = self._settings.stripe_secret_key
            self._stripe = stripe
        return self._stripe

    def create_customer(
        self,
        *,
        email: str,
        name: str,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        stripe = self._client()
        customer = stripe.Customer.create(email=email, name=name, metadata=metadata or {})
        return {"id": customer.id, "email": customer.email}

    def create_checkout_session(
        self,
        *,
        customer_id: str,
        price_id: str,
        success_url: str,
        cancel_url: str,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        stripe = self._client()
        session = stripe.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=metadata or {},
            subscription_data={"metadata": metadata or {}},
        )
        return {"id": session.id, "url": session.url}

    def create_portal_session(self, *, customer_id: str, return_url: str) -> dict[str, Any]:
        stripe = self._client()
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url,
        )
        return {"url": session.url}

    def construct_webhook_event(self, payload: bytes, sig_header: str) -> dict[str, Any]:
        stripe = self._client()
        secret = self._settings.stripe_webhook_secret
        if not secret:
            raise StripeNotConfiguredError("STRIPE_WEBHOOK_SECRET not configured")
        event = stripe.Webhook.construct_event(payload, sig_header, secret)
        return event.to_dict() if hasattr(event, "to_dict") else dict(event)


def ensure_stripe_customer(
    session,
    tenant_id: UUID,
    *,
    email: str,
    name: str,
) -> str:
    """首次 checkout 创建 Stripe Customer 并持久化（C2-04）。"""
    from src.billing.models import Tenant

    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise ValueError("tenant not found")
    if tenant.stripe_customer_id:
        return tenant.stripe_customer_id
    client = StripeClient()
    customer = client.create_customer(
        email=email,
        name=name,
        metadata={"tenant_id": str(tenant_id)},
    )
    tenant.stripe_customer_id = customer["id"]
    session.commit()
    return customer["id"]
