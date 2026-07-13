"""商机洞察 — 独立 FastAPI 应用（默认端口 8091）。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.web.agri_insights_service import (
    get_agri_insights_service,
    get_agri_ui_port,
    get_main_ui_port,
)
from src.web.biz_clue_service import get_biz_clue_service
from src.core.agri_sites import agri_site_display_list
from src.web.crawl_agent_routes import register_crawl_agent_routes
from src.web.data_service import get_data_service

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="WebScraperAgent",
    description="WebScraperAgent — 商机洞察专题",
)
app.add_middleware(GZipMiddleware, minimum_size=500)
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

_jinja_env = Environment(
    loader=FileSystemLoader(WEB_DIR / "templates"),
    autoescape=select_autoescape(["html", "xml"]),
)

_sync_lock = asyncio.Lock()
_sync_running = False
_last_sync_summary: dict[str, Any] | None = None


def _agri_page_context(**extra: Any) -> dict[str, Any]:
    return {
        "main_ui_port": get_main_ui_port(),
        "agri_ui_port": get_agri_ui_port(),
        **extra,
    }


def _render(name: str, context: dict[str, Any], *, status_code: int = 200) -> HTMLResponse:
    template = _jinja_env.get_template(name)
    return HTMLResponse(template.render(**context), status_code=status_code)


register_crawl_agent_routes(
    app,
    render=_render,
    default_agent_profile="agri",
    chat_template="agri_hermes.html",
    include_chat_alias=False,
    include_pending_page=False,
)


async def _run_agri_sync_job(
    *,
    site_id: Optional[str] = None,
    max_items: Optional[int] = None,
) -> None:
    global _sync_running, _last_sync_summary
    from src.core.daily_agri_sync import run_daily_agri_sync
    from src.core.scheduler import load_yaml

    schedule_path = Path(__file__).resolve().parent.parent.parent / "config" / "schedule.yaml"
    schedule_config = load_yaml(schedule_path) if schedule_path.exists() else {}
    try:
        summary = await run_daily_agri_sync(
            schedule_config=schedule_config,
            site_id=site_id,
            max_items=max_items,
            update_status=True,
        )
        _last_sync_summary = summary
    except Exception as exc:
        logger.exception("农业同步失败: %s", exc)
        _last_sync_summary = {"success": False, "error": str(exc)}
    finally:
        _sync_running = False


@app.get("/", response_class=HTMLResponse)
async def agri_page(
    request: Request,
    days: int = Query(30, ge=1, le=90),
    refresh: bool = Query(False, description="忽略缓存重新分析"),
    stage: Optional[str] = None,
    product_line: Optional[str] = None,
    level: Optional[str] = None,
):
    svc = get_data_service()
    biz_ctx = get_biz_clue_service().get_page_context(
        days=days, refresh=refresh
    )
    for _dup in ("days",):
        biz_ctx.pop(_dup, None)
    leads_data = get_biz_clue_service().get_leads(
        days=days,
        stage=(stage or "").strip() or None,
        product_line=(product_line or "").strip() or None,
        level=(level or "").strip() or None,
        limit=30,
    )
    page_ctx = _agri_page_context(
        request=request,
        data_source=svc.data_source,
        sync_running=_sync_running,
        active_nav="insights",
        days=days,
        filter_stage=stage,
        filter_product_line=product_line,
        filter_level=level,
        filtered_leads=leads_data.get("items") or [],
        **biz_ctx,
    )
    return _render("agri.html", page_ctx)


@app.get("/tenders", response_class=HTMLResponse)
async def agri_tenders_page(
    request: Request,
    days: int = Query(30, ge=1, le=90),
    keyword: Optional[str] = None,
    region: Optional[str] = None,
    category: Optional[str] = None,
    min_score: int = Query(0, ge=0, le=100),
    tier: Optional[str] = None,
    site_id: Optional[str] = None,
    sort: str = Query("score"),
    limit: int = Query(50, ge=1, le=200),
):
    svc = get_data_service()
    tenders = get_agri_insights_service().get_tenders(
        days=days,
        limit=limit,
        keyword=(keyword or "").strip() or None,
        region=(region or "").strip() or None,
        category=(category or "").strip() or None,
        min_score=min_score,
        tier=(tier or "").strip() or None,
        site_id=(site_id or "").strip() or None,
        sort=sort if sort in ("score", "date") else "score",
    )
    page_ctx = _agri_page_context(
        request=request,
        data_source=svc.data_source,
        active_nav="tenders",
        days=days,
        tenders=tenders,
        mvp_sites=agri_site_display_list(),
        filters=tenders.get("filters") or {},
    )
    return _render("agri_tenders.html", page_ctx)


@app.get("/api/agri/tenders")
async def api_agri_tenders(
    days: int = Query(30, ge=1, le=90),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    site_id: Optional[str] = None,
    keyword: Optional[str] = None,
    region: Optional[str] = None,
    category: Optional[str] = None,
    min_score: int = Query(0, ge=0, le=100),
    tier: Optional[str] = None,
    sort: str = Query("score"),
):
    svc = get_data_service()
    result = get_agri_insights_service().get_tenders(
        days=days,
        limit=limit,
        offset=offset,
        site_id=(site_id or "").strip() or None,
        keyword=(keyword or "").strip() or None,
        region=(region or "").strip() or None,
        category=(category or "").strip() or None,
        min_score=min_score,
        tier=(tier or "").strip() or None,
        sort=sort if sort in ("score", "date") else "score",
    )
    return {"data_source": svc.data_source, **result}


@app.get("/api/agri/latest")
async def api_agri_latest(
    limit: int = Query(20, ge=1, le=100),
    days: int = Query(7, ge=1, le=90),
    site_id: Optional[str] = None,
):
    svc = get_data_service()
    result = get_agri_insights_service().get_latest(limit=limit, days=days, site_id=site_id)
    return {"data_source": svc.data_source, **result}


@app.get("/api/agri/insights")
async def api_agri_insights(
    days: int = Query(7, ge=1, le=90),
    site_id: Optional[str] = None,
):
    svc = get_data_service()
    result = get_agri_insights_service().get_insights(
        days=days,
        site_id=site_id,
    )
    return {"data_source": svc.data_source, **result}


@app.post("/api/agri/sync")
async def api_agri_sync(
    background_tasks: BackgroundTasks,
    site_id: Optional[str] = None,
    max_items: Optional[int] = None,
):
    global _sync_running
    if _sync_running:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": "农业同步任务已在运行",
                "running": True,
                "last_summary": _last_sync_summary,
            },
        )
    _sync_running = True
    background_tasks.add_task(
        _run_agri_sync_job,
        site_id=(site_id or "").strip() or None,
        max_items=max_items,
    )
    return {
        "ok": True,
        "message": "农业同步已启动（后台执行）",
        "running": True,
    }


@app.get("/api/agri/sync/status")
async def api_agri_sync_status():
    return {
        "ok": True,
        "running": _sync_running,
        "last_summary": _last_sync_summary,
    }


@app.get("/api/agri/health")
async def api_agri_health():
    return {
        "ok": True,
        "service": "agri_ui",
        "main_ui_port": get_main_ui_port(),
        "sync_running": _sync_running,
    }


@app.get("/api/biz-clue/summary")
async def api_biz_clue_summary(
    days: int = Query(30, ge=1, le=90),
    site_id: Optional[str] = None,
):
    svc = get_data_service()
    result = get_biz_clue_service().get_dashboard_summary(
        days=days, site_id=(site_id or "").strip() or None
    )
    return {"data_source": svc.data_source, **result}


@app.get("/api/biz-clue/leads")
async def api_biz_clue_leads(
    days: int = Query(30, ge=1, le=90),
    site_id: Optional[str] = None,
    stage: Optional[str] = None,
    product_line: Optional[str] = None,
    level: Optional[str] = None,
    verification: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    svc = get_data_service()
    result = get_biz_clue_service().get_leads(
        days=days,
        site_id=(site_id or "").strip() or None,
        stage=(stage or "").strip() or None,
        product_line=(product_line or "").strip() or None,
        level=(level or "").strip() or None,
        verification=(verification or "").strip() or None,
        limit=limit,
        offset=offset,
    )
    return {"data_source": svc.data_source, **result}


@app.get("/api/biz-clue/brief")
async def api_biz_clue_brief(
    days: int = Query(30, ge=1, le=90),
    site_id: Optional[str] = None,
):
    svc = get_data_service()
    result = get_biz_clue_service().generate_daily_brief(
        days=days, site_id=(site_id or "").strip() or None
    )
    return {"data_source": svc.data_source, **result}


@app.post("/api/biz-clue/analyze")
async def api_biz_clue_analyze(
    days: int = Query(30, ge=1, le=90),
    site_id: Optional[str] = None,
):
    svc = get_data_service()
    result = get_biz_clue_service().analyze_collected_data(
        days=days,
        site_id=(site_id or "").strip() or None,
        refresh=True,
    )
    return {"data_source": svc.data_source, "ok": True, **result}
