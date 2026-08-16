"""JWT service tests."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from jose import jwt

from src.billing.jwt_service import JWTService


def test_create_and_decode_access_token():
    svc = JWTService(secret="test-secret-key", algorithm="HS256", access_ttl_minutes=15)
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    token, expires_in = svc.create_access_token(
        user_id=user_id,
        tenant_id=tenant_id,
        roles=["admin"],
    )
    assert expires_in == 15 * 60
    payload = svc.decode_access_token(token)
    assert payload["sub"] == str(user_id)
    assert payload["tenant_id"] == str(tenant_id)
    assert payload["roles"] == ["admin"]


def test_expired_token_raises():
    svc = JWTService(secret="test-secret-key", algorithm="HS256", access_ttl_minutes=-1)
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    token, _ = svc.create_access_token(user_id=user_id, tenant_id=tenant_id, roles=["member"])
    with pytest.raises(ValueError, match="invalid or expired"):
        svc.decode_access_token(token)


def test_tampered_token_rejected():
    svc = JWTService(secret="test-secret-key")
    token, _ = svc.create_access_token(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        roles=["viewer"],
    )
    payload = jwt.get_unverified_claims(token)
    bad = jwt.encode(payload, "wrong-secret", algorithm="HS256")
    with pytest.raises(ValueError):
        svc.decode_access_token(bad)
