"""Usage metering — record & aggregate."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.billing.models import Tenant, UsageAggregate, UsageEvent
from src.billing.plans import get_entitlement_limit
from src.billing.schemas import MetricUsage, UsageSummaryResponse
from src.core.config import get_settings

logger = logging.getLogger(__name__)

CORE_METRICS = (
    "api_calls",
    "crawl_runs",
    "llm_extraction_tokens",
    "llm_chat_tokens",
    "hermes_messages",
    "storage_bytes",
)


def current_period_key(dt: datetime | None = None) -> str:
    when = dt or datetime.now(timezone.utc)
    return when.strftime("%Y-%m")


def daily_period_key(dt: datetime | None = None) -> str:
    when = dt or datetime.now(timezone.utc)
    return when.strftime("%Y-%m-%d")


def period_key_for_metric(metric: str, dt: datetime | None = None) -> str:
    if metric == "crawl_runs":
        return daily_period_key(dt)
    return current_period_key(dt)


class UsageService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        *,
        tenant_id: UUID,
        metric: str,
        quantity: float | Decimal = 1,
        user_id: UUID | None = None,
        unit: str = "count",
        period_key: str | None = None,
        idempotency_key: str | None = None,
        source: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UsageEvent | None:
        if not get_settings().billing_enabled:
            return None
        if not get_settings().database_url:
            logger.debug("billing usage skipped: no DATABASE_URL")
            return None

        event = UsageEvent(
            tenant_id=tenant_id,
            user_id=user_id,
            metric=metric,
            quantity=Decimal(str(quantity)),
            unit=unit,
            period_key=period_key or current_period_key(),
            idempotency_key=idempotency_key,
            source=source,
            metadata_=metadata or {},
        )
        self.session.add(event)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            if idempotency_key:
                return self.session.scalar(
                    select(UsageEvent).where(
                        UsageEvent.tenant_id == tenant_id,
                        UsageEvent.idempotency_key == idempotency_key,
                    )
                )
            raise
        self.session.refresh(event)
        return event

    def sum_metric(self, tenant_id: UUID, metric: str, period_key: str) -> Decimal:
        agg = self.session.scalar(
            select(UsageAggregate.total_quantity).where(
                UsageAggregate.tenant_id == tenant_id,
                UsageAggregate.metric == metric,
                UsageAggregate.period_key == period_key,
            )
        )
        if agg is not None:
            return Decimal(str(agg))
        total = self.session.scalar(
            select(func.coalesce(func.sum(UsageEvent.quantity), 0)).where(
                UsageEvent.tenant_id == tenant_id,
                UsageEvent.metric == metric,
                UsageEvent.period_key == period_key,
            )
        )
        return Decimal(str(total or 0))

    def get_usage_summary(self, tenant_id: UUID, period_key: str | None = None) -> UsageSummaryResponse:
        period = period_key or current_period_key()
        tenant = self.session.get(Tenant, tenant_id)
        plan_id = tenant.plan_id if tenant else "free"
        metrics: dict[str, MetricUsage] = {}

        metric_specs = {
            "crawl_runs": ("runs/day", "daily"),
            "llm_extraction_tokens": ("tokens", "monthly"),
            "storage_gb": ("GB", "monthly"),
            "hermes_messages": ("messages", "monthly"),
            "api_calls": ("calls", "monthly"),
        }

        for metric, (unit, period_type) in metric_specs.items():
            db_metric = metric
            lookup_period = period_key_for_metric(db_metric) if period_type == "daily" else period
            if metric == "storage_gb":
                used = self.sum_metric(tenant_id, "storage_bytes", period) / Decimal("1073741824")
            else:
                used = self.sum_metric(tenant_id, db_metric, lookup_period)
            limit = get_entitlement_limit(plan_id, metric)
            metrics[metric] = MetricUsage(
                used=float(used),
                limit=limit,
                unit=unit,
                period=period_type,
            )

        return UsageSummaryResponse(
            tenant_id=tenant_id,
            period=period,
            plan_id=plan_id,
            metrics=metrics,
        )


_usage_service: UsageService | None = None


def record_usage(
    *,
    tenant_id: UUID | None,
    metric: str,
    quantity: float = 1,
    user_id: UUID | None = None,
    source: str | None = None,
    metadata: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    period_key: str | None = None,
) -> None:
    """Fire-and-forget usage hook; safe when billing disabled."""
    if not get_settings().billing_enabled or tenant_id is None:
        return
    try:
        from src.billing.session import billing_session_scope

        with billing_session_scope() as session:
            UsageService(session).record(
                tenant_id=tenant_id,
                metric=metric,
                quantity=quantity,
                user_id=user_id,
                source=source,
                metadata=metadata,
                idempotency_key=idempotency_key,
                period_key=period_key or period_key_for_metric(metric),
            )
    except Exception:
        logger.exception("failed to record usage metric=%s tenant=%s", metric, tenant_id)
