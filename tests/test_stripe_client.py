"""Stripe client 单元测试（C2-01）。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.billing.stripe_client import StripeClient, StripeNotConfiguredError


def test_stripe_not_configured():
    with patch("src.billing.stripe_client.get_settings") as mock_settings:
        mock_settings.return_value.stripe_secret_key = None
        client = StripeClient()
        assert client.configured is False
        with pytest.raises(StripeNotConfiguredError):
            client.create_customer(email="a@b.com", name="Test")


def test_create_checkout_session_mock():
    client = StripeClient()
    mock_stripe = MagicMock()
    mock_session = MagicMock(id="cs_test", url="https://checkout.stripe.com/test")
    mock_stripe.checkout.Session.create.return_value = mock_session
    with patch("src.billing.stripe_client.get_settings") as mock_settings:
        mock_settings.return_value.stripe_secret_key = "sk_test"
        with patch.object(client, "_client", return_value=mock_stripe):
            result = client.create_checkout_session(
                customer_id="cus_1",
                price_id="price_1",
                success_url="https://ok",
                cancel_url="https://cancel",
                metadata={"tenant_id": "t1"},
            )
    assert result["url"] == "https://checkout.stripe.com/test"
