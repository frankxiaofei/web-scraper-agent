"""Auth API routes — register, login, me."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from src.billing.auth_service import (
    EmailAlreadyExistsError,
    InvalidCredentialsError,
    RefreshTokenError,
    get_me,
    login_user,
    refresh_access_token,
    register_user,
)
from src.billing.email_service import consume_verification_token, create_verification_token, send_verification_email
from src.billing.models import User
from src.billing.schemas import (
    LoginRequest,
    LoginResponse,
    MeResponse,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
)
from src.billing.session import billing_database_configured, get_billing_session
from src.core.config import get_settings
from src.web.auth.deps import AuthContext, require_auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _require_db() -> None:
    if not billing_database_configured():
        raise HTTPException(status_code=503, detail="DATABASE_URL required for auth")


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
def auth_register(body: RegisterRequest) -> RegisterResponse:
    _require_db()
    session = get_billing_session()
    try:
        result = register_user(
            session,
            email=str(body.email),
            password=body.password,
            display_name=body.display_name,
            org_name=body.org_name,
        )
        token = create_verification_token(str(body.email))
        send_verification_email(str(body.email), token, get_settings().public_base_url)
        return result
    except EmailAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail="email already registered") from exc
    finally:
        session.close()


@router.post("/login", response_model=LoginResponse)
def auth_login(body: LoginRequest) -> LoginResponse:
    _require_db()
    session = get_billing_session()
    try:
        return login_user(
            session,
            email=str(body.email),
            password=body.password,
            tenant_id=body.tenant_id,
        )
    except InvalidCredentialsError as exc:
        raise HTTPException(status_code=401, detail="invalid email or password") from exc
    finally:
        session.close()


@router.post("/refresh", response_model=TokenResponse)
def auth_refresh(body: RefreshRequest) -> TokenResponse:
    _require_db()
    session = get_billing_session()
    try:
        access_token, expires_in, refresh_token = refresh_access_token(session, refresh_token=body.refresh_token)
        return TokenResponse(
            access_token=access_token,
            expires_in=expires_in,
            refresh_token=refresh_token,
        )
    except RefreshTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    finally:
        session.close()


@router.get("/me", response_model=MeResponse)
def auth_me(auth: AuthContext = Depends(require_auth)) -> MeResponse:
    _require_db()
    session = get_billing_session()
    try:
        return get_me(session, user_id=auth.user_id, tenant_id=auth.tenant_id)
    except InvalidCredentialsError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    finally:
        session.close()


@router.get("/verify-email")
def verify_email(token: str = Query(...)) -> dict[str, str | bool]:
    _require_db()
    email = consume_verification_token(token)
    if email is None:
        raise HTTPException(status_code=400, detail="invalid or expired verification token")
    session = get_billing_session()
    try:
        user = session.scalar(select(User).where(User.email == email))
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        user.email_verified_at = datetime.now(timezone.utc)
        session.commit()
        return {"ok": True, "email": email}
    finally:
        session.close()
