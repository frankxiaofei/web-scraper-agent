"""行业洞察 API 路由（Phase 0）。"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import unquote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.web.industry_heatmap_service import HeatmapLevel, HeatmapMetric, get_industry_heatmap_service
from src.web.industry_insights_service import get_industry_insights_service
from src.web.industry_policy_bid_service import get_industry_policy_bid_service

router = APIRouter(prefix="/api/industry", tags=["industry"])


def _require_industry_heatmap(request: Request) -> None:
    from src.billing.quota_middleware import enforce_entitlement, tenant_from_request

    enforce_entitlement(tenant_from_request(request), "feature.industry_heatmap")


class IndustrySyncRequest(BaseModel):
    domain: Optional[str] = "数字农业"
    site_ids: list[str] = Field(default_factory=list)
    max_items: int = 30


@router.get("/summary")
async def industry_summary(
    domain: str = Query("数字农业"),
    days: int = Query(30, ge=1, le=90),
):
    svc = get_industry_insights_service()
    try:
        return svc.summary(domain=domain, days=days)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/distribution")
async def industry_distribution(
    domain: str = Query("数字农业"),
    days: int = Query(30, ge=1, le=90),
    level: str = Query("province"),
):
    svc = get_industry_insights_service()
    try:
        return svc.distribution(domain=domain, days=days, level=level)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/taxonomy")
async def industry_taxonomy(
    domain: str = Query("数字农业"),
    taxonomy: str = Query("PSEUDO"),
):
    if taxonomy.upper() in {"NAICS2022", "NAICS", "ISIC4", "GB2017"} and taxonomy.upper() != "PSEUDO":
        from src.industry.global_data.taxonomy_views import build_taxonomy_view

        try:
            return build_taxonomy_view(taxonomy)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    svc = get_industry_insights_service()
    try:
        return svc.taxonomy(domain=domain, taxonomy=taxonomy)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/trade-flows")
async def industry_trade_flows(
    reporter: str = Query("156"),
    period: str = Query("2024"),
    commodity_code: str = Query("10"),
):
    from src.industry.global_data.comtrade_sync import fetch_trade_flows

    flows = fetch_trade_flows(reporter=reporter, period=period, commodity_code=commodity_code)
    return {"reporter": reporter, "period": period, "flows": flows, "count": len(flows)}


@router.get("/commercial-supply-chain")
async def industry_commercial_supply_chain(
    company: str = Query(..., min_length=2),
    limit: int = Query(20, ge=1, le=100),
):
    from src.industry.global_data.commercial_supply_chain import fetch_commercial_supply_chain

    return fetch_commercial_supply_chain(company, limit=limit)


@router.get("/recommendations")
async def industry_recommendations(
    domain: str = Query("数字农业"),
    days: int = Query(30, ge=1, le=90),
    limit: int = Query(6, ge=1, le=20),
):
    svc = get_industry_insights_service()
    try:
        items = svc.recommend_industries(domain=domain, days=days, limit=limit)
        return {"domain": domain, "days": days, "items": items}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/policy-bid-lag")
async def industry_policy_bid_lag(
    domain: str = Query("数字农业"),
    lag_days: int = Query(90, ge=7, le=365),
    days: int = Query(180, ge=30, le=365),
):
    return get_industry_policy_bid_service().policy_bid_lag(
        domain=domain,
        lag_days=lag_days,
        days=days,
    )


@router.get("/policy-bid-trend")
async def industry_policy_bid_trend(
    theme: str = Query(...),
    days: int = Query(180, ge=30, le=365),
    bucket: str = Query("week"),
    domain: str = Query("数字农业"),
):
    try:
        return get_industry_policy_bid_service().policy_bid_trend(
            theme,
            days=days,
            bucket=bucket,
            domain=domain,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sync")
async def industry_sync(
    background_tasks: BackgroundTasks,
    body: IndustrySyncRequest | None = None,
):
    """桥接 POST /api/biz-clue/sync。"""
    import src.web.app as web_app
    from src.web.biz_clue_service import check_sync_quota

    check_sync_quota()
    if web_app._biz_clue_sync_running:
        return {"ok": True, "task_id": "biz-clue-sync", "status": "running"}
    web_app._biz_clue_sync_running = True
    site_ids = (body.site_ids if body else []) or []
    site_id = site_ids[0] if len(site_ids) == 1 else None
    max_items = body.max_items if body else 30
    background_tasks.add_task(
        web_app._run_biz_clue_sync_job,
        site_id=site_id,
        max_items=max_items,
    )
    return {"ok": True, "task_id": "biz-clue-sync", "status": "started"}


@router.get("/sync/status")
async def industry_sync_status():
    import src.web.app as web_app

    status = "running" if web_app._biz_clue_sync_running else "idle"
    return {"status": status, "task_id": "biz-clue-sync", "message": status}


@router.get("/graph")
async def industry_graph(
    center: str = Query("", description="中心企业 company_id (UUID)"),
    depth: int = Query(2, ge=1, le=4),
    industry_code: str = Query("", description="按行业 code 展开子图"),
    limit: int = Query(200, ge=1, le=500),
):
    from src.industry.graph.neo4j_sync import get_neo4j_graph_service

    svc = get_neo4j_graph_service()
    return svc.query_subgraph(
        center=center or None,
        depth=depth,
        industry_code=industry_code or None,
        limit=limit,
    )


@router.get("/{industry_code}/heatmap")
async def industry_heatmap(
    industry_code: str,
    metric: str = Query("count"),
    level: str = Query("province"),
    days: int = Query(30, ge=1, le=90),
    domain: str = Query("数字农业"),
    region: str = Query(""),
    _: None = Depends(_require_industry_heatmap),
):
    code = unquote(industry_code)
    svc = get_industry_heatmap_service()
    try:
        return svc.heatmap(
            code,
            metric=HeatmapMetric(metric),
            level=HeatmapLevel(level),
            days=days,
            domain=domain,
            region=region,
        )
    except ValueError as exc:
        msg = str(exc)
        status = 404 if "unknown domain" in msg else 400
        raise HTTPException(status_code=status, detail=msg) from exc


@router.get("/{industry_code}/heatmap/notices")
async def industry_heatmap_notices(
    industry_code: str,
    region: str = Query(..., description="省级 region_code，如 CN-44"),
    days: int = Query(30, ge=1, le=90),
    domain: str = Query("数字农业"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _: None = Depends(_require_industry_heatmap),
):
    code = unquote(industry_code)
    svc = get_industry_heatmap_service()
    try:
        return svc.region_notices(
            code,
            region,
            days=days,
            domain=domain,
            page=page,
            page_size=page_size,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{industry_code}/supply-chain")
async def industry_supply_chain(
    industry_code: str,
    days: int = Query(30, ge=1, le=90),
    domain: str = Query("数字农业"),
    limit: int = Query(50, ge=1, le=200),
):
    code = unquote(industry_code)
    svc = get_industry_insights_service()
    try:
        return svc.supply_chain_table(code, days=days, domain=domain, limit=limit)
    except ValueError as exc:
        msg = str(exc)
        status = 404 if "unknown domain" in msg else 400
        raise HTTPException(status_code=status, detail=msg) from exc
