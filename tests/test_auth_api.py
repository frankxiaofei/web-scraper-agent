"""Auth & billing API handler tests (mocked DB layer)."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from src.billing.auth_service import EmailAlreadyExistsError, InvalidCredentialsError
from src.billing.schemas import (
    LoginRequest,
    LoginResponse,
    MeResponse,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    TenantSummary,
    TokenResponse,
    UserSummary,
)
from src.web.auth.deps import AuthContext
from src.web.auth_routes import auth_login, auth_me, auth_refresh, auth_register
from src.web.billing_routes import billing_plans, billing_usage


def test_billing_plans_handler():
    resp = billing_plans()
    ids = {p.id for p in resp.plans}
    assert "free" in ids


@patch("src.web.auth_routes.billing_database_configured", return_value=False)
def test_register_without_database_returns_503(_mock_db):
    with pytest.raises(HTTPException) as exc:
        auth_register(
            RegisterRequest(email="new@example.com", password="SecureP@ss1", display_name="Test")
        )
    assert exc.value.status_code == 503


@patch("src.web.auth_routes.billing_database_configured", return_value=True)
@patch("src.web.auth_routes.get_billing_session")
@patch("src.web.auth_routes.register_user")
@patch("src.web.auth_routes.create_verification_token", return_value="tok")
@patch("src.web.auth_routes.send_verification_email")
def test_register_success(_send, _tok, mock_register, mock_session, _mock_db):
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    mock_register.return_value = RegisterResponse(
        user_id=user_id,
        tenant_id=tenant_id,
        email="bd@example.com",
        email_verification_required=True,
    )
    mock_session.return_value = MagicMock()
    result = auth_register(
        RegisterRequest(email="bd@example.com", password="SecureP@ss1", display_name="张三")
    )
    assert result.email == "bd@example.com"


@patch("src.web.auth_routes.billing_database_configured", return_value=True)
@patch("src.web.auth_routes.get_billing_session")
@patch("src.web.auth_routes.register_user", side_effect=EmailAlreadyExistsError("dup"))
def test_register_duplicate_email_409(mock_register, mock_session, _mock_db):
    with pytest.raises(HTTPException) as exc:
        auth_register(
            RegisterRequest(email="dup@example.com", password="SecureP@ss1")
        )
    assert exc.value.status_code == 409


@patch("src.web.auth_routes.billing_database_configured", return_value=True)
@patch("src.web.auth_routes.get_billing_session")
@patch("src.web.auth_routes.login_user")
def test_login_success(mock_login, mock_session, _mock_db):
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    mock_login.return_value = LoginResponse(
        access_token="access.test",
        expires_in=900,
        refresh_token="refresh.test",
        user=UserSummary(id=user_id, email="bd@example.com", display_name="张三"),
        tenant=TenantSummary(id=tenant_id, name="Org", role="admin", plan_id="free"),
    )
    mock_session.return_value = MagicMock()
    result = auth_login(LoginRequest(email="bd@example.com", password="SecureP@ss1"))
    assert result.access_token == "access.test"


@patch("src.web.auth_routes.billing_database_configured", return_value=True)
@patch("src.web.auth_routes.get_billing_session")
@patch("src.web.auth_routes.login_user", side_effect=InvalidCredentialsError("bad"))
def test_login_invalid_401(mock_login, mock_session, _mock_db):
    with pytest.raises(HTTPException) as exc:
        auth_login(LoginRequest(email="bd@example.com", password="wrong"))
    assert exc.value.status_code == 401


def test_me_without_auth_dependency():
    auth = AuthContext(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), roles=["admin"])
    with patch("src.web.auth_routes.billing_database_configured", return_value=True):
        with patch("src.web.auth_routes.get_billing_session") as mock_session:
            with patch("src.web.auth_routes.get_me") as mock_get_me:
                mock_session.return_value = MagicMock()
                user_id = auth.user_id
                tenant_id = auth.tenant_id
                mock_get_me.return_value = MeResponse(
                    user=UserSummary(id=user_id, email="bd@example.com"),
                    tenants=[TenantSummary(id=tenant_id, name="Org", role="admin", plan_id="free")],
                    current_tenant=TenantSummary(id=tenant_id, name="Org", role="admin", plan_id="free"),
                )
                result = auth_me(auth=auth)
                assert result.user.email == "bd@example.com"


@patch("src.web.auth_routes.billing_database_configured", return_value=True)
@patch("src.web.auth_routes.get_billing_session")
@patch("src.web.auth_routes.refresh_access_token")
def test_refresh_token(mock_refresh, mock_session, _mock_db):
    mock_refresh.return_value = ("new.access", 900, "new.refresh")
    mock_session.return_value = MagicMock()
    result = auth_refresh(RefreshRequest(refresh_token="old.refresh"))
    assert isinstance(result, TokenResponse)
    assert result.access_token == "new.access"


@patch("src.web.billing_routes.billing_database_configured", return_value=True)
@patch("src.web.billing_routes.get_billing_session")
@patch("src.web.billing_routes.UsageService")
def test_billing_usage_handler(mock_usage_cls, mock_session, _mock_db):
    tenant_id = uuid.uuid4()
    auth = AuthContext(user_id=uuid.uuid4(), tenant_id=tenant_id, roles=["admin"])
    mock_session.return_value = MagicMock()
    from src.billing.schemas import MetricUsage, UsageSummaryResponse

    mock_usage_cls.return_value.get_usage_summary.return_value = UsageSummaryResponse(
        tenant_id=tenant_id,
        period="2026-08",
        plan_id="free",
        metrics={"crawl_runs": MetricUsage(used=2, limit=5, unit="runs/day", period="daily")},
    )
    result = billing_usage(auth=auth, period=None)
    assert result.metrics["crawl_runs"].used == 2
