"""Quota enforcement tests (C1-09~13)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from src.billing.quota_middleware import check_entitlement, enforce_entitlement


def test_enforce_skipped_when_billing_disabled(monkeypatch):
    monkeypatch.setenv("BILLING_ENABLED", "false")
    result = enforce_entitlement(uuid.uuid4(), "quota.crawl_runs")
    assert result.allowed is True


@patch("src.billing.quota_middleware._billing_active", return_value=True)
@patch("src.billing.quota_middleware.billing_session_scope")
def test_crawl_quota_exceeded_403(mock_scope, _active):
    tenant_id = uuid.uuid4()
    session = MagicMock()
    mock_scope.return_value.__enter__.return_value = session
    from src.billing.entitlements import EntitlementService

    with patch.object(EntitlementService, "check") as mock_check:
        from src.billing.entitlements import EntitlementResult

        mock_check.return_value = EntitlementResult(
            allowed=False,
            used=5.0,
            limit=5,
            plan_id="free",
            key="quota.crawl_runs",
        )
        with pytest.raises(HTTPException) as exc:
            enforce_entitlement(tenant_id, "quota.crawl_runs")
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "quota_exceeded"


@patch("src.billing.quota_middleware._billing_active", return_value=True)
@patch("src.billing.quota_middleware.billing_session_scope")
def test_feature_heatmap_denied_free(mock_scope, _active):
    tenant_id = uuid.uuid4()
    session = MagicMock()
    mock_scope.return_value.__enter__.return_value = session
    from src.billing.entitlements import EntitlementService, EntitlementResult

    with patch.object(EntitlementService, "check") as mock_check:
        mock_check.return_value = EntitlementResult(
            allowed=False,
            used=0.0,
            limit=None,
            plan_id="free",
            key="feature.industry_heatmap",
        )
        result = check_entitlement(tenant_id, "feature.industry_heatmap")
    assert result.allowed is False
