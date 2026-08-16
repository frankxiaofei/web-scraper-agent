"""Request-scoped tenant context via contextvars."""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Optional

_current_tenant_id: ContextVar[Optional[uuid.UUID]] = ContextVar("billing_tenant_id", default=None)
_current_user_id: ContextVar[Optional[uuid.UUID]] = ContextVar("billing_user_id", default=None)


def get_current_tenant_id() -> Optional[uuid.UUID]:
    return _current_tenant_id.get()


def get_current_user_id() -> Optional[uuid.UUID]:
    return _current_user_id.get()


def set_current_tenant_id(tenant_id: uuid.UUID | None) -> None:
    _current_tenant_id.set(tenant_id)


def set_current_user_id(user_id: uuid.UUID | None) -> None:
    _current_user_id.set(user_id)
