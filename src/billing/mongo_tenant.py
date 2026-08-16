"""MongoDB tenant_id field helpers — C0 skeleton."""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from src.core.config import get_settings


def inject_tenant_id(document: dict[str, Any], tenant_id: Optional[UUID] = None) -> dict[str, Any]:
    """Add tenant_id to new documents when billing is enabled."""
    if not get_settings().billing_enabled:
        return document
    if tenant_id is None:
        from src.billing.tenant_context import get_current_tenant_id

        tenant_id = get_current_tenant_id()
    if tenant_id is not None:
        document = dict(document)
        document["tenant_id"] = str(tenant_id)
    return document


def tenant_filter(tenant_id: Optional[UUID] = None) -> dict[str, Any]:
    """MongoDB query filter for current tenant when billing enabled."""
    if not get_settings().billing_enabled:
        return {}
    if tenant_id is None:
        from src.billing.tenant_context import get_current_tenant_id

        tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return {}
    return {"tenant_id": str(tenant_id)}
