"""Billing webhook 处理测试（C2-05~C2-07）。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.billing.models import Subscription, Tenant
from src.billing.webhook_handlers import handle_stripe_event


def test_webhook_idempotent():
    session = MagicMock()
    session.scalar.return_value = 1
    event = {"id": "evt_dup", "type": "invoice.paid", "data": {"object": {}}}
    result = handle_stripe_event(session, event)
    assert result.get("duplicate") is True


def test_checkout_completed_activates_subscription():
    tenant_id = uuid.uuid4()
    session = MagicMock()
    session.scalar.side_effect = [None, None]
    session.get.return_value = MagicMock(id=tenant_id, plan_id="free")

    with patch("src.billing.webhook_handlers.subscribe_tenant") as mock_subscribe:
        mock_subscribe.return_value = MagicMock(
            payment_provider="manual",
            stripe_subscription_id=None,
        )
        with patch("src.billing.webhook_handlers._tenant_from_metadata") as mock_tenant:
            mock_tenant.return_value = MagicMock(id=tenant_id)
            event = {
                "id": "evt_checkout",
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "metadata": {"tenant_id": str(tenant_id), "plan_id": "pro"},
                        "subscription": "sub_stripe_1",
                    }
                },
            }
            result = handle_stripe_event(session, event)
    assert result.get("handled") is True
