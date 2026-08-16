"""UsageService aggregate preference tests."""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import MagicMock

from src.billing.usage_service import UsageService


def test_sum_metric_prefers_aggregate():
    session = MagicMock()
    session.scalar.side_effect = [Decimal("99"), None]
    svc = UsageService(session)
    total = svc.sum_metric(uuid.uuid4(), "api_calls", "2026-08")
    assert total == Decimal("99")
