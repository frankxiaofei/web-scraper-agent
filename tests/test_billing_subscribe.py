"""Billing subscribe API 测试。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from src.billing.schemas import SubscribeRequest
from src.billing.subscription_service import UnknownPlanError
from src.web.auth.deps import AuthContext
from src.web.billing_routes import billing_subscribe


def test_subscribe_admin_success():
    tenant_id = uuid.uuid4()
    auth = AuthContext(user_id=uuid.uuid4(), tenant_id=tenant_id, roles=["admin"])
    mock_sub = MagicMock()
    mock_sub.plan_id = "pro"
    mock_sub.status = "active"
    mock_sub.billing_cycle = "monthly"
    mock_sub.current_period_start = datetime.now(timezone.utc)
    mock_sub.current_period_end = datetime.now(timezone.utc)
    mock_sub.cancel_at_period_end = False
    mock_session = MagicMock()
    with patch("src.web.billing_routes.billing_database_configured", return_value=True):
        with patch("src.web.billing_routes.get_billing_session", return_value=mock_session):
            with patch("src.web.billing_routes.subscribe_tenant", return_value=mock_sub):
                resp = billing_subscribe(SubscribeRequest(plan_id="pro"), auth=auth)
    assert resp.plan_id == "pro"
    mock_session.close.assert_called_once()


def test_subscribe_unknown_plan_400():
    tenant_id = uuid.uuid4()
    auth = AuthContext(user_id=uuid.uuid4(), tenant_id=tenant_id, roles=["admin"])
    mock_session = MagicMock()
    with patch("src.web.billing_routes.billing_database_configured", return_value=True):
        with patch("src.web.billing_routes.get_billing_session", return_value=mock_session):
            with patch(
                "src.web.billing_routes.subscribe_tenant",
                side_effect=UnknownPlanError("unknown plan: bad"),
            ):
                with pytest.raises(HTTPException) as exc:
                    billing_subscribe(SubscribeRequest(plan_id="bad"), auth=auth)
    assert exc.value.status_code == 400
