"""Tenant context helpers."""

from __future__ import annotations

import uuid
from typing import Optional

from starlette.requests import Request


def get_request_tenant_id(request: Request) -> Optional[uuid.UUID]:
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        return None
    if isinstance(tenant_id, uuid.UUID):
        return tenant_id
    return uuid.UUID(str(tenant_id))


def set_request_tenant_id(request: Request, tenant_id: uuid.UUID) -> None:
    request.state.tenant_id = tenant_id
