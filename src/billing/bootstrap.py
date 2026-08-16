"""Register billing routers & middleware on FastAPI app."""

from __future__ import annotations

from fastapi import FastAPI

from src.core.config import get_settings
from src.web.auth.middleware import AuthMiddleware
from src.web.auth.usage_middleware import UsageMeteringMiddleware
from src.web.auth_routes import router as auth_router
from src.web.billing_routes import router as billing_router


def register_billing(app: FastAPI) -> None:
    """Mount auth/billing API; enable middleware only when BILLING_ENABLED=true."""
    app.include_router(auth_router)
    app.include_router(billing_router)
    settings = get_settings()
    if settings.billing_enabled:
        app.add_middleware(UsageMeteringMiddleware)
        app.add_middleware(AuthMiddleware)
