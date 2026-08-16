"""Pydantic schemas — auth & billing API."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

RoleType = Literal["admin", "member", "viewer"]


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=128)
    org_name: str | None = Field(default=None, max_length=256)

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, value: str) -> str:
        if not re.search(r"[A-Z]", value):
            raise ValueError("password must contain an uppercase letter")
        if not re.search(r"[a-z]", value):
            raise ValueError("password must contain a lowercase letter")
        if not re.search(r"\d", value):
            raise ValueError("password must contain a digit")
        return value


class RegisterResponse(BaseModel):
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    email_verification_required: bool = True


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    tenant_id: uuid.UUID | None = None


class UserSummary(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None = None
    email_verified: bool = False


class TenantSummary(BaseModel):
    id: uuid.UUID
    name: str
    role: RoleType
    plan_id: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_token: str
    user: UserSummary
    tenant: TenantSummary


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_token: str


class MeResponse(BaseModel):
    user: UserSummary
    tenants: list[TenantSummary]
    current_tenant: TenantSummary | None = None


class MetricUsage(BaseModel):
    used: float
    limit: int | None = None
    unit: str
    period: str


class UsageSummaryResponse(BaseModel):
    tenant_id: uuid.UUID
    period: str
    plan_id: str
    metrics: dict[str, MetricUsage]


class PlanEntitlements(BaseModel):
    enabled_sites: int = 10
    crawl_runs_per_day: int = 5
    llm_tokens_per_month: int = 50_000
    hermes_messages_per_month: int = 20
    storage_gb: int = 1
    webbridge_minutes_per_month: int = 0
    gov_bid_api_calls: int = 0
    team_members: int = 1
    features: list[str] = Field(default_factory=list)


class PlanResponse(BaseModel):
    id: str
    name: str
    price_monthly_cny: int
    price_yearly_cny: int
    entitlements: PlanEntitlements


class PlansListResponse(BaseModel):
    plans: list[PlanResponse]


class SubscribeRequest(BaseModel):
    plan_id: str = Field(min_length=1, max_length=32)
    billing_cycle: Literal["monthly", "yearly"] = "monthly"


class SubscriptionResponse(BaseModel):
    tenant_id: uuid.UUID
    plan_id: str
    status: str
    billing_cycle: str
    current_period_start: str
    current_period_end: str
    cancel_at_period_end: bool = False


class CancelSubscriptionResponse(BaseModel):
    ok: bool = True
    cancel_at_period_end: bool = True
    current_period_end: str


class CheckoutSessionRequest(BaseModel):
    plan_id: str = Field(default="pro", min_length=1, max_length=32)
    billing_cycle: Literal["monthly", "yearly"] = "monthly"
    success_url: str = "/settings/billing?checkout=success"
    cancel_url: str = "/settings/billing?checkout=cancel"


class CheckoutSessionResponse(BaseModel):
    checkout_url: str


class PortalSessionResponse(BaseModel):
    portal_url: str


class InvoiceSummary(BaseModel):
    id: uuid.UUID
    number: str
    status: str
    amount_cny: int
    currency: str
    period_start: str
    period_end: str
    paid_at: str | None = None
    pdf_url: str | None = None


class InvoiceListResponse(BaseModel):
    invoices: list[InvoiceSummary]
    total: int


class BuyerInfoRequest(BaseModel):
    buyer_name: str = Field(min_length=1, max_length=256)
    buyer_tax_id: str | None = Field(default=None, max_length=32)


class UsageRecordRequest(BaseModel):
    tenant_id: uuid.UUID
    metric: str
    quantity: float = 1.0
    user_id: uuid.UUID | None = None
    unit: str = "count"
    period_key: str | None = None
    idempotency_key: str | None = None
    source: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
