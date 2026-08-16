"""Pydantic schema validation tests."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from src.billing.schemas import LoginRequest, RegisterRequest, UsageSummaryResponse


def test_register_request_valid():
    req = RegisterRequest(email="bd@example.com", password="SecureP@ss1", display_name="张三")
    assert req.email == "bd@example.com"


def test_register_request_weak_password():
    with pytest.raises(ValidationError):
        RegisterRequest(email="bd@example.com", password="short1")


def test_login_request_accepts_optional_tenant():
    tid = uuid.uuid4()
    req = LoginRequest(email="a@b.com", password="x", tenant_id=tid)
    assert req.tenant_id == tid


def test_usage_summary_serialization():
    summary = UsageSummaryResponse(
        tenant_id=uuid.uuid4(),
        period="2026-08",
        plan_id="free",
        metrics={},
    )
    data = summary.model_dump()
    assert data["plan_id"] == "free"
