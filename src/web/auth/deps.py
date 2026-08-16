"""Auth & RBAC FastAPI dependencies."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.billing.auth_service import InvalidCredentialsError, get_me
from src.billing.jwt_service import get_jwt_service
from src.billing.schemas import MeResponse
from src.billing.session import get_billing_session
from src.core.config import get_settings

_bearer = HTTPBearer(auto_error=False)

RoleLevel = {"viewer": 0, "member": 1, "admin": 2}


class AuthContext:
    def __init__(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        roles: list[str],
    ) -> None:
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.roles = roles

    @property
    def primary_role(self) -> str:
        if "admin" in self.roles:
            return "admin"
        if "member" in self.roles:
            return "member"
        return "viewer"


async def get_optional_auth_context(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthContext | None:
    if hasattr(request.state, "auth") and request.state.auth is not None:
        return request.state.auth
    if credentials is None or not credentials.credentials:
        return None
    try:
        payload = get_jwt_service().decode_access_token(credentials.credentials)
        return AuthContext(
            user_id=uuid.UUID(payload["sub"]),
            tenant_id=uuid.UUID(payload["tenant_id"]),
            roles=list(payload.get("roles") or []),
        )
    except (ValueError, KeyError):
        return None


async def require_auth(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthContext:
    ctx = await get_optional_auth_context(request, credentials)
    if ctx is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
    request.state.auth = ctx
    request.state.tenant_id = ctx.tenant_id
    request.state.user_id = ctx.user_id
    return ctx


def require_role(min_role: Literal["viewer", "member", "admin"]):
    async def _dep(auth: Annotated[AuthContext, Depends(require_auth)]) -> AuthContext:
        role = auth.primary_role
        if RoleLevel[role] < RoleLevel[min_role]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
        return auth

    return _dep


async def get_me_response(auth: Annotated[AuthContext, Depends(require_auth)]) -> MeResponse:
    session = get_billing_session()
    try:
        return get_me(session, user_id=auth.user_id, tenant_id=auth.tenant_id)
    except InvalidCredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    finally:
        session.close()
