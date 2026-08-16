"""Email verification stub — C0 logs token; C1+ sends SMTP."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_verification_tokens: dict[str, tuple[str, datetime]] = {}


def create_verification_token(email: str) -> str:
    token = secrets.token_urlsafe(32)
    _verification_tokens[token] = (email.lower(), datetime.now(timezone.utc) + timedelta(hours=24))
    logger.info("email verification token for %s: %s", email, token)
    return token


def consume_verification_token(token: str) -> str | None:
    entry = _verification_tokens.pop(token, None)
    if entry is None:
        return None
    email, expires_at = entry
    if expires_at < datetime.now(timezone.utc):
        return None
    return email


def send_verification_email(email: str, token: str, base_url: str) -> None:
    link = f"{base_url.rstrip('/')}/api/auth/verify-email?token={token}"
    logger.info("verification email -> %s link=%s", email, link)
