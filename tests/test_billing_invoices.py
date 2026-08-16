"""Billing invoices API 测试（C2-10）。"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from src.billing.models import Invoice
from src.web.auth.deps import AuthContext
from src.web.billing_routes import billing_invoice_pdf, billing_invoices


def test_invoices_list_empty():
    tenant_id = uuid.uuid4()
    auth = AuthContext(user_id=uuid.uuid4(), tenant_id=tenant_id, roles=["admin"])
    mock_session = MagicMock()
    mock_session.scalar.return_value = 0
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []
    mock_session.scalars.return_value = mock_scalars
    with patch("src.web.billing_routes.billing_database_configured", return_value=True):
        with patch("src.web.billing_routes.get_billing_session", return_value=mock_session):
            resp = billing_invoices(auth=auth, limit=20, offset=0)
    assert resp.total == 0
    assert resp.invoices == []


def test_render_invoice_pdf_bytes():
    from types import SimpleNamespace

    from src.billing.invoice_pdf import render_invoice_pdf

    inv = SimpleNamespace(
        number="INV-001",
        status="paid",
        amount_cny=9900,
        tax_amount_cny=0,
        period_start="2026-08-01",
        period_end="2026-08-31",
        buyer_name=None,
        buyer_tax_id=None,
        paid_at=None,
        tenant_id=uuid.uuid4(),
    )
    pdf = render_invoice_pdf(inv)
    assert pdf.startswith(b"%PDF")
