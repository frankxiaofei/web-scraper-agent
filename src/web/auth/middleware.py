"""JWT auth middleware — optional when BILLING_ENABLED=true."""

from __future__ import annotations

import re
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.billing.jwt_service import get_jwt_service
from src.billing.tenant_context import set_current_tenant_id, set_current_user_id
from src.web.auth.deps import AuthContext

PUBLIC_PATHS = (
    "/",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/static",
    "/data",
    "/extensions",
    "/api/auth/register",
    "/api/auth/login",
    "/api/auth/refresh",
    "/api/auth/verify-email",
    "/api/auth/forgot-password",
    "/api/auth/reset-password",
    "/api/billing/plans",
    "/api/billing/webhook",
)

PUBLIC_PREFIXES = (
    "/static/",
    "/data/",
    "/extensions/",
)


def _is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def _extract_bearer(request: Request) -> str | None:
    header = request.headers.get("Authorization")
    if not header:
        return None
    match = re.match(r"^Bearer\s+(.+)$", header, re.I)
    return match.group(1) if match else None


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if _is_public_path(path) or not path.startswith("/api/"):
            return await call_next(request)

        token = _extract_bearer(request)
        if not token:
            return JSONResponse(status_code=401, content={"detail": "authentication required"})

        try:
            payload = get_jwt_service().decode_access_token(token)
            ctx = AuthContext(
                user_id=uuid.UUID(payload["sub"]),
                tenant_id=uuid.UUID(payload["tenant_id"]),
                roles=list(payload.get("roles") or []),
            )
        except (ValueError, KeyError):
            return JSONResponse(status_code=401, content={"detail": "invalid or expired token"})

        request.state.auth = ctx
        request.state.tenant_id = ctx.tenant_id
        request.state.user_id = ctx.user_id
        set_current_tenant_id(ctx.tenant_id)
        set_current_user_id(ctx.user_id)
        readonly_block = _past_due_write_block(request, ctx)
        if readonly_block is not None:
            return readonly_block
        try:
            return await call_next(request)
        finally:
            set_current_tenant_id(None)
            set_current_user_id(None)


_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _past_due_write_block(request: Request, ctx: AuthContext) -> JSONResponse | None:
    """past_due 租户只允许 GET/HEAD（C2-08）。"""
    if request.method not in _WRITE_METHODS:
        return None
    if request.url.path.startswith("/api/billing/webhook"):
        return None
    try:
        from src.billing.entitlements import is_tenant_readonly

        if is_tenant_readonly(ctx.tenant_id):
            return JSONResponse(
                status_code=403,
                content={"detail": "subscription past_due: read-only mode"},
            )
    except Exception:
        return None
    return None
