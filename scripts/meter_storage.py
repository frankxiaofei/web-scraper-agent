#!/usr/bin/env python3
"""Daily storage metering cron — estimates MongoDB collection size."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.billing.session import billing_session_scope, init_billing_schema
from src.billing.usage_service import UsageService, current_period_key
from src.core.config import get_settings

logger = logging.getLogger(__name__)


def estimate_mongo_bytes() -> int:
    settings = get_settings()
    if not settings.mongodb_uri:
        return 0
    try:
        from src.db.mongo_client import get_mongo_client

        client = get_mongo_client()
        db = client[settings.mongodb_db]
        stats = db.command("collStats", settings.mongodb_collection)
        return int(stats.get("storageSize") or stats.get("size") or 0)
    except Exception as exc:
        logger.warning("mongo storage estimate failed: %s", exc)
        return 0


def meter_all_tenants() -> None:
    settings = get_settings()
    if not settings.billing_enabled or not settings.database_url:
        logger.info("billing disabled or no DATABASE_URL; skip storage metering")
        return

    init_billing_schema()
    mongo_bytes = estimate_mongo_bytes()
    if mongo_bytes <= 0:
        logger.info("no mongo storage to meter")
        return

    from sqlalchemy import select

    from src.billing.models import Tenant

    period = current_period_key()
    with billing_session_scope() as session:
        tenant_ids = session.scalars(select(Tenant.id)).all()
        svc = UsageService(session)
        for tenant_id in tenant_ids:
            svc.record(
                tenant_id=tenant_id,
                metric="storage_bytes",
                quantity=mongo_bytes,
                unit="bytes",
                period_key=period,
                idempotency_key=f"storage:{tenant_id}:{period}",
                source="cron",
                metadata={"mongo_bytes": mongo_bytes},
            )
    logger.info("storage metering complete: %d bytes across %d tenants", mongo_bytes, len(tenant_ids))


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Meter MongoDB storage for billing tenants")
    parser.parse_args()
    meter_all_tenants()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
