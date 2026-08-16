"""世舶招投标 API 的服务端代理路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.core.gov_bid_client import (
    GovBidAPIError,
    GovBidConfigurationError,
    get_gov_bid_client,
)

router = APIRouter(prefix="/api/gov-bid", tags=["gov-bid"])


class SearchProjectsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    startDate: str
    endDate: str
    userID: int = 2
    pageID: int = Field(default=1, ge=1)
    pageNumber: int = Field(default=20, ge=1, le=50)
    searchType: int = Field(default=1, ge=1, le=3)


class ProjectRequest(BaseModel):
    id: int
    publishTime: str


class CompanyProfileRequest(BaseModel):
    companyName: str = Field(min_length=1)


def _gov_bid_quota(request: Request) -> None:
    from src.billing.quota_middleware import consume_entitlement, tenant_from_request

    consume_entitlement(tenant_from_request(request), "quota.gov_bid_api")


async def _call(awaitable):
    try:
        return await awaitable
    except GovBidConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except GovBidAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/status")
async def gov_bid_status() -> dict[str, Any]:
    client = get_gov_bid_client()
    return {"ok": True, "configured": client.configured, "base_url": client.base_url}


@router.post("/projects/search")
async def search_projects(body: SearchProjectsRequest, _: None = Depends(_gov_bid_quota)):
    return await _call(get_gov_bid_client().search_projects(body.model_dump(exclude_none=True)))


@router.post("/projects/structure")
async def project_structure(body: ProjectRequest, _: None = Depends(_gov_bid_quota)):
    return await _call(get_gov_bid_client().get_structure(body.id, body.publishTime))


@router.post("/projects/detail")
async def project_detail(body: ProjectRequest, _: None = Depends(_gov_bid_quota)):
    return await _call(get_gov_bid_client().get_project_detail(body.id, body.publishTime))


@router.post("/projects/files")
async def project_files(body: ProjectRequest, _: None = Depends(_gov_bid_quota)):
    return await _call(get_gov_bid_client().get_project_files(body.id, body.publishTime))


@router.post("/companies/profile")
async def company_profile(body: CompanyProfileRequest = Body(...), _: None = Depends(_gov_bid_quota)):
    return await _call(get_gov_bid_client().get_company_profile(body.companyName.strip()))
