"""Ontology action API（Phase 1 + Phase 2）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.industry.graph.neo4j_sync import get_neo4j_graph_service
from src.industry.mdm.repository import get_mdm_repository

router = APIRouter(prefix="/api/ontology", tags=["ontology"])


class ConfirmIndustryRequest(BaseModel):
    company_id: uuid.UUID
    industry_code: str = Field(min_length=1, max_length=32)
    taxonomy: str = "GB2017"


class MergeCompaniesRequest(BaseModel):
    source_ids: list[uuid.UUID] = Field(min_length=1)
    target_id: uuid.UUID
    reason: str | None = None


@router.post("/actions/merge-companies")
async def merge_companies_action(body: MergeCompaniesRequest):
    """合并重复企业实体（P2-T-008）。"""
    mdm = get_mdm_repository()
    if not mdm or not mdm.schema_ready():
        raise HTTPException(status_code=503, detail="MDM schema not available")
    try:
        result = mdm.merge_companies(body.source_ids, body.target_id, reason=body.reason)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    graph = get_neo4j_graph_service()
    if graph.configured:
        graph.merge_companies(
            source_ids=[str(x) for x in body.source_ids],
            target_id=str(body.target_id),
        )
    return {"ok": True, **result}


@router.post("/actions/confirm-industry")
async def confirm_industry_classification(body: ConfirmIndustryRequest):
    """人工确认企业行业分类（confidence=1.0, method=manual）。"""
    mdm = get_mdm_repository()
    if not mdm or not mdm.schema_ready():
        raise HTTPException(status_code=503, detail="MDM schema not available")

    mdm.upsert_company_industry(
        body.company_id,
        body.industry_code,
        body.taxonomy,
        confidence=1.0,
        method="manual",
    )
    mdm.record_lineage(
        "company",
        str(body.company_id),
        "confirm_industry",
        {"industry_code": body.industry_code, "taxonomy": body.taxonomy},
    )
    return {
        "ok": True,
        "company_id": str(body.company_id),
        "industry_code": body.industry_code,
        "taxonomy": body.taxonomy,
        "confidence": 1.0,
        "method": "manual",
    }
