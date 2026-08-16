"""API call usage metering middleware."""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from src.billing.usage_service import record_usage
from src.core.config import get_settings


class UsageMeteringMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        if not get_settings().billing_enabled:
            return response
        if not request.url.path.startswith("/api/"):
            return response
        tenant_id = getattr(request.state, "tenant_id", None)
        if tenant_id is None:
            return response
        user_id = getattr(request.state, "user_id", None)
        record_usage(
            tenant_id=tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id)),
            metric="api_calls",
            quantity=1,
            user_id=user_id if isinstance(user_id, uuid.UUID) or user_id is None else uuid.UUID(str(user_id)),
            source="api",
            metadata={"path": request.url.path, "method": request.method, "status": response.status_code},
        )
        return response
