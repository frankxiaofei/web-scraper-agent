"""RBAC dependency tests."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException

from src.web.auth.deps import AuthContext, RoleLevel, require_role


def test_require_role_admin_passes():
    auth = AuthContext(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), roles=["admin"])
    dep = require_role("member")
    result = asyncio.run(dep(auth))
    assert result.primary_role == "admin"


def test_require_role_viewer_blocked_from_admin():
    auth = AuthContext(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), roles=["viewer"])
    dep = require_role("admin")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(dep(auth))
    assert exc.value.status_code == 403


def test_role_levels_order():
    assert RoleLevel["viewer"] < RoleLevel["admin"]
