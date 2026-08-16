"""Auth business logic — register, login, refresh."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.billing.jwt_service import get_jwt_service
from src.billing.models import RefreshToken, Subscription, Tenant, TenantMember, User
from src.billing.password import hash_password, verify_password
from src.billing.schemas import LoginResponse, MeResponse, RegisterResponse, TenantSummary, UserSummary


class EmailAlreadyExistsError(Exception):
    pass


class InvalidCredentialsError(Exception):
    pass


class RefreshTokenError(Exception):
    pass


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "org"


def _unique_slug(session: Session, base: str) -> str:
    slug = _slugify(base)
    candidate = slug
    suffix = 1
    while session.scalar(select(Tenant.id).where(Tenant.slug == candidate)):
        candidate = f"{slug}-{suffix}"
        suffix += 1
    return candidate


def _hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _issue_refresh_token(session: Session, user_id: uuid.UUID, *, ttl_days: int = 7) -> str:
    raw = secrets.token_urlsafe(48)
    token_hash = _hash_refresh_token(raw)
    expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    session.add(
        RefreshToken(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
    )
    return raw


def register_user(
    session: Session,
    *,
    email: str,
    password: str,
    display_name: str | None = None,
    org_name: str | None = None,
) -> RegisterResponse:
    user = User(
        email=email.lower().strip(),
        password_hash=hash_password(password),
        display_name=display_name,
    )
    org_label = org_name or display_name or email.split("@")[0]
    tenant = Tenant(
        name=org_label,
        slug=_unique_slug(session, email.split("@")[0]),
        plan_id="free",
        billing_email=email.lower().strip(),
    )
    session.add(user)
    session.flush()
    session.add(tenant)
    session.flush()
    session.add(
        TenantMember(
            tenant_id=tenant.id,
            user_id=user.id,
            role="admin",
        )
    )
    now = datetime.now(timezone.utc)
    session.add(
        Subscription(
            tenant_id=tenant.id,
            plan_id="free",
            status="active",
            billing_cycle="monthly",
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
        )
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise EmailAlreadyExistsError("email already registered") from exc
    session.refresh(user)
    session.refresh(tenant)
    return RegisterResponse(
        user_id=user.id,
        tenant_id=tenant.id,
        email=user.email,
        email_verification_required=True,
    )


def login_user(
    session: Session,
    *,
    email: str,
    password: str,
    tenant_id: uuid.UUID | None = None,
) -> LoginResponse:
    user = session.scalar(select(User).where(User.email == email.lower().strip(), User.is_active.is_(True)))
    if user is None or not verify_password(password, user.password_hash):
        raise InvalidCredentialsError("invalid email or password")

    membership_query = (
        select(TenantMember, Tenant)
        .join(Tenant, Tenant.id == TenantMember.tenant_id)
        .where(TenantMember.user_id == user.id)
    )
    if tenant_id is not None:
        membership_query = membership_query.where(TenantMember.tenant_id == tenant_id)
    row = session.execute(membership_query).first()
    if row is None:
        raise InvalidCredentialsError("no tenant membership found")

    member, tenant = row
    user.last_login_at = datetime.now(timezone.utc)
    jwt_svc = get_jwt_service()
    access_token, expires_in = jwt_svc.create_access_token(
        user_id=user.id,
        tenant_id=tenant.id,
        roles=[member.role],
    )
    refresh_token = _issue_refresh_token(session, user.id)
    session.commit()

    return LoginResponse(
        access_token=access_token,
        expires_in=expires_in,
        refresh_token=refresh_token,
        user=UserSummary(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            email_verified=user.email_verified_at is not None,
        ),
        tenant=TenantSummary(
            id=tenant.id,
            name=tenant.name,
            role=member.role,  # type: ignore[arg-type]
            plan_id=tenant.plan_id,
        ),
    )


def refresh_access_token(session: Session, *, refresh_token: str) -> tuple[str, int, str]:
    token_hash = _hash_refresh_token(refresh_token)
    row = session.scalar(
        select(RefreshToken).where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked_at.is_(None),
        )
    )
    if row is None or row.expires_at < datetime.now(timezone.utc):
        raise RefreshTokenError("invalid refresh token")

    row.revoked_at = datetime.now(timezone.utc)
    membership = session.execute(
        select(TenantMember, Tenant)
        .join(Tenant, Tenant.id == TenantMember.tenant_id)
        .where(TenantMember.user_id == row.user_id)
        .limit(1)
    ).first()
    if membership is None:
        raise RefreshTokenError("no tenant membership")

    member, tenant = membership
    jwt_svc = get_jwt_service()
    access_token, expires_in = jwt_svc.create_access_token(
        user_id=row.user_id,
        tenant_id=tenant.id,
        roles=[member.role],
    )
    new_refresh = _issue_refresh_token(session, row.user_id)
    session.commit()
    return access_token, expires_in, new_refresh


def get_me(session: Session, *, user_id: uuid.UUID, tenant_id: uuid.UUID | None = None) -> MeResponse:
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        raise InvalidCredentialsError("user not found")

    rows = session.execute(
        select(TenantMember, Tenant).join(Tenant, Tenant.id == TenantMember.tenant_id).where(
            TenantMember.user_id == user_id
        )
    ).all()
    tenants = [
        TenantSummary(
            id=tenant.id,
            name=tenant.name,
            role=member.role,  # type: ignore[arg-type]
            plan_id=tenant.plan_id,
        )
        for member, tenant in rows
    ]
    current = next((t for t in tenants if tenant_id and t.id == tenant_id), tenants[0] if tenants else None)
    return MeResponse(
        user=UserSummary(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            email_verified=user.email_verified_at is not None,
        ),
        tenants=tenants,
        current_tenant=current,
    )
