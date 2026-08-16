"""JWT access token issue & verify."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt

from src.core.config import get_settings


class JWTService:
    def __init__(
        self,
        *,
        secret: str | None = None,
        algorithm: str | None = None,
        access_ttl_minutes: int | None = None,
    ) -> None:
        settings = get_settings()
        self.secret = secret or settings.jwt_secret or "dev-insecure-jwt-secret-change-me"
        self.algorithm = algorithm or settings.jwt_algorithm
        self.access_ttl_minutes = access_ttl_minutes or settings.jwt_access_ttl_minutes

    def create_access_token(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        roles: list[str],
        extra: dict[str, Any] | None = None,
    ) -> tuple[str, int]:
        expires_delta = timedelta(minutes=self.access_ttl_minutes)
        now = datetime.now(timezone.utc)
        expire = now + expires_delta
        payload: dict[str, Any] = {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "roles": roles,
            "iat": int(now.timestamp()),
            "exp": int(expire.timestamp()),
        }
        if extra:
            payload.update(extra)
        token = jwt.encode(payload, self.secret, algorithm=self.algorithm)
        return token, int(expires_delta.total_seconds())

    def decode_access_token(self, token: str) -> dict[str, Any]:
        try:
            return jwt.decode(token, self.secret, algorithms=[self.algorithm])
        except JWTError as exc:
            raise ValueError("invalid or expired token") from exc


_jwt_service: JWTService | None = None


def get_jwt_service() -> JWTService:
    global _jwt_service
    if _jwt_service is None:
        _jwt_service = JWTService()
    return _jwt_service
