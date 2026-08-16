#!/usr/bin/env python3
"""Hourly cron: aggregate billing.usage_events → usage_aggregates.

Usage:
  python scripts/aggregate_usage.py
  python scripts/aggregate_usage.py --period 2026-08
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.billing.session import billing_database_configured, billing_session_scope
from src.billing.usage_aggregator import aggregate_all_recent, aggregate_usage_for_period
from src.core.config import get_settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate billing usage_events")
    parser.add_argument("--period", help="Specific period_key (YYYY-MM or YYYY-MM-DD)")
    args = parser.parse_args()

    if not get_settings().billing_enabled:
        print("BILLING_ENABLED=false — skipping aggregation")
        return 0
    if not billing_database_configured():
        print("DATABASE_URL not configured", file=sys.stderr)
        return 1

    with billing_session_scope() as session:
        if args.period:
            count = aggregate_usage_for_period(session, args.period)
            print(f"aggregated period={args.period} rows={count}")
        else:
            results = aggregate_all_recent(session)
            for period, count in results.items():
                print(f"aggregated period={period} rows={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
