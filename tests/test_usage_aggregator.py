"""Usage aggregator tests (C1-08)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

from src.billing.usage_aggregator import aggregate_usage_for_period
from src.billing.usage_service import daily_period_key


def test_daily_period_key_format():
    key = daily_period_key()
    assert len(key) == 10
    assert key.count("-") == 2


@patch("src.billing.usage_aggregator.datetime")
def test_aggregate_usage_for_period(mock_dt):
    mock_dt.now.return_value = __import__("datetime").datetime(2026, 8, 16, tzinfo=__import__("datetime").timezone.utc)
    session = MagicMock()
    tenant_id = __import__("uuid").uuid4()
    rows_result = MagicMock()
    rows_result.all.return_value = [(tenant_id, "crawl_runs", "2026-08-16", Decimal("5"))]
    session.execute.return_value = rows_result
    count = aggregate_usage_for_period(session, "2026-08-16")
    assert count == 1
    session.commit.assert_called_once()
