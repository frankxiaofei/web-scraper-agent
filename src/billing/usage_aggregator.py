"""Hourly aggregation of usage_events → usage_aggregates (Commercial C1-08)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from src.billing.models import UsageAggregate, UsageEvent
from src.billing.usage_service import CORE_METRICS, current_period_key, daily_period_key

logger = logging.getLogger(__name__)

_DAILY_METRICS = frozenset({"crawl_runs"})


def period_keys_for_metric(metric: str, *, now: datetime | None = None) -> list[str]:
    """Return period keys that should be aggregated for a metric."""
    when = now or datetime.now(timezone.utc)
    keys = [current_period_key(when)]
    if metric in _DAILY_METRICS:
        keys.append(daily_period_key(when))
    return keys


def aggregate_usage_for_period(session: Session, period_key: str) -> int:
    """Aggregate usage_events into usage_aggregates for one period_key. Returns row count."""
    rows = session.execute(
        select(
            UsageEvent.tenant_id,
            UsageEvent.metric,
            UsageEvent.period_key,
            func.coalesce(func.sum(UsageEvent.quantity), 0).label("total"),
        )
        .where(UsageEvent.period_key == period_key)
        .group_by(UsageEvent.tenant_id, UsageEvent.metric, UsageEvent.period_key)
    ).all()

    upserted = 0
    for tenant_id, metric, pk, total in rows:
        stmt = insert(UsageAggregate).values(
            tenant_id=tenant_id,
            metric=metric,
            period_key=pk,
            total_quantity=Decimal(str(total or 0)),
            updated_at=datetime.now(timezone.utc),
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_usage_aggregate",
            set_={
                "total_quantity": stmt.excluded.total_quantity,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        session.execute(stmt)
        upserted += 1

    session.commit()
    logger.info("usage aggregate period=%s rows=%d", period_key, upserted)
    return upserted


def aggregate_all_recent(session: Session) -> dict[str, int]:
    """Aggregate current month + today (for daily metrics). Returns period→count map."""
    now = datetime.now(timezone.utc)
    periods = {current_period_key(now), daily_period_key(now)}
    results: dict[str, int] = {}
    for period in sorted(periods):
        results[period] = aggregate_usage_for_period(session, period)
    return results


def aggregate_metrics(session: Session, metrics: tuple[str, ...] = CORE_METRICS) -> int:
    """Aggregate all period keys touched by the given metrics."""
    now = datetime.now(timezone.utc)
    periods: set[str] = set()
    for metric in metrics:
        periods.update(period_keys_for_metric(metric, now=now))
    total = 0
    for period in sorted(periods):
        total += aggregate_usage_for_period(session, period)
    return total
