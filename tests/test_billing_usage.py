"""UsageService unit tests."""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

from src.billing.usage_service import UsageService, current_period_key, record_usage


def test_current_period_key_format():
    assert len(current_period_key()) == 7
    assert current_period_key().__contains__("-")


def test_record_usage_skipped_when_billing_disabled(monkeypatch):
    monkeypatch.setenv("BILLING_ENABLED", "false")
    record_usage(tenant_id=uuid.uuid4(), metric="api_calls")


@patch("src.billing.usage_service.get_settings")
def test_usage_service_sum_metric(mock_settings):
    mock_settings.return_value.billing_enabled = True
    mock_settings.return_value.database_url = "postgresql://localhost/test"
    session = MagicMock()
    session.scalar.return_value = Decimal("42")
    svc = UsageService(session)
    total = svc.sum_metric(uuid.uuid4(), "crawl_runs", "2026-08")
    assert total == Decimal("42")
