"""招标公告 Web UI — FastAPI 应用。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
import mimetypes

from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel

from urllib.parse import quote_plus

from src.agent.crawl_logger import get_agent_crawl_logger
from src.agent.task_manager import get_task_manager
from src.core.config import get_settings
from src.core.rule_generator import (
    get_rule_generator,
    load_rule_yaml,
    save_rule_yaml,
    validate_yaml,
)
from src.core.site_sync import get_site_by_id
from src.web.data_service import get_data_service
from src.web.schedule_service import get_schedule_service
from src.web.script_cron_service import get_script_cron_service
from src.web.sync_service import get_sync_service
from src.web.stats_service import get_stats_service
from src.core.agri_sites import agri_site_display_list
from src.web.agri_insights_service import DEFAULT_AGRI_DAYS, MAX_AGRI_DAYS, get_agri_insights_service
from src.web.biz_clue_service import get_biz_clue_service
from src.web.task_service import get_task_service
from src.web.sites_service import get_sites_service
from src.web.settings_service import get_public_settings, save_settings

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = WEB_DIR.parent.parent
SKILLS_DIR = PROJECT_ROOT / "skills"


def _list_project_skills() -> list[dict[str, str]]:
    """扫描 skills/*/SKILL.md，供 Hermes 对话页展示可用 Skill 文档。"""
    if not SKILLS_DIR.is_dir():
        return []
    items: list[dict[str, str]] = []
    for path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        skill_id = path.parent.name
        items.append(
            {
                "id": skill_id,
                "path": f"skills/{skill_id}/SKILL.md",
            }
        )
    return items


app = FastAPI(title="WebScraperAgent", description="WebScraperAgent — 多源站点爬取与招标洞察")

app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8090",
        "http://localhost:8090",
        "http://127.0.0.1:8091",
        "http://localhost:8091",
    ],
    allow_origin_regex=r"chrome-extension://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

DATA_DIR = PROJECT_ROOT / "data"
if DATA_DIR.is_dir():
    app.mount("/data", StaticFiles(directory=DATA_DIR), name="data")

EXTENSIONS_DIR = PROJECT_ROOT / "extensions"
if EXTENSIONS_DIR.is_dir():
    app.mount(
        "/extensions",
        StaticFiles(directory=EXTENSIONS_DIR),
        name="extensions",
    )

from src.web.crawl_recorder_routes import router as crawl_recorder_router

app.include_router(crawl_recorder_router)

_jinja_env = Environment(
    loader=FileSystemLoader(WEB_DIR / "templates"),
    autoescape=select_autoescape(["html", "xml"]),
)
_jinja_env.filters["urlencode"] = lambda s: quote_plus(str(s))


@app.on_event("startup")
async def _detect_stale_sync_on_startup() -> None:
    sync_svc = get_sync_service()
    stale = sync_svc.detect_stale_syncs()
    if stale:
        logger.warning("检测到 %d 个 stale syncing 状态（服务重启后内存任务丢失）: %s", len(stale), stale)


def _base_template_context() -> dict[str, Any]:
    from src.web.ui_context import build_base_template_context

    return build_base_template_context(is_agri_service=False)


def _render(name: str, context: dict[str, Any], *, status_code: int = 200) -> HTMLResponse:
    template = _jinja_env.get_template(name)
    return HTMLResponse(
        template.render(**_base_template_context(), **context),
        status_code=status_code,
    )


@app.get("/")
async def home_redirect():
    return RedirectResponse(url="/hermes", status_code=302)


@app.get("/list", response_class=HTMLResponse)
async def notice_list(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    q: Optional[str] = None,
    site_ids: list[str] = Query(default=[]),
    tags: list[str] = Query(default=[]),
    source: Optional[str] = Query(None, description="已废弃，请使用 site_ids"),
    tag: Optional[str] = Query(None, description="已废弃，请使用 tags"),
):
    svc = get_data_service()
    result = svc.list_notices(
        page=page,
        per_page=per_page,
        site_ids=site_ids or None,
        keyword=q,
        tags=tags or None,
        source=source,
        tag=tag,
        has_content=False,
    )
    resolved_site_ids, resolved_tags = svc._resolve_list_filters(
        source=source,
        site_ids=site_ids,
        tag=tag,
        tags=tags,
    )
    return _render(
        "list.html",
        {
            "request": request,
            "notices": result["items"],
            "pagination": result,
            "sources": svc.list_sources(),
            "tags": svc.list_tags(),
            "current_site_ids": resolved_site_ids,
            "current_tags": resolved_tags,
            "keyword": q or "",
            "advanced_open": bool(resolved_site_ids or resolved_tags),
            "data_source": svc.data_source,
            "active_nav": "list",
        },
    )


_WECHAT_ASSET_SITE_RE = re.compile(r"^wechat_[a-z0-9_]+$")


@app.get("/api/wechat-assets/{site_id}/{file_path:path}")
async def serve_wechat_asset(site_id: str, file_path: str):
    """仅暴露 data/{site_id}/images/ 下微信正文图片，禁止目录穿越。"""
    if not _WECHAT_ASSET_SITE_RE.fullmatch(site_id):
        raise HTTPException(status_code=403, detail="非法站点")
    rel = Path(file_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise HTTPException(status_code=400, detail="非法路径")
    base = (DATA_DIR / site_id / "images").resolve()
    target = (base / rel).resolve()
    if not str(target).startswith(str(base)) or not target.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
    media_type, _ = mimetypes.guess_type(target.name)
    return FileResponse(target, media_type=media_type or "application/octet-stream")


@app.get("/notices/{notice_id}", response_class=HTMLResponse)
async def notice_detail(request: Request, notice_id: str):
    svc = get_data_service()
    notice = svc.get_notice(notice_id)
    if not notice:
        raise HTTPException(status_code=404, detail="公告不存在")
    return _render(
        "detail.html",
        {
            "request": request,
            "notice": notice,
            "all_tags": svc.list_tags(),
            "data_source": svc.data_source,
        },
    )


@app.get("/api/notices")
async def api_list_notices(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    source: Optional[str] = None,
    source_site_id: Optional[str] = None,
    site_ids: list[str] = Query(default=[]),
    q: Optional[str] = None,
    has_content: bool = Query(True, description="仅返回有正文的公告"),
    agri_only: bool = Query(False, description="仅返回数字农业相关公告"),
    bim_only: bool = Query(False, description="已废弃，等同 agri_only"),
    tag: Optional[str] = Query(None, description="按标签筛选，如 微信公众号"),
    tags: list[str] = Query(default=[]),
):
    svc = get_data_service()
    effective_source = source or source_site_id
    result = svc.list_notices(
        page=page,
        per_page=per_page,
        source=effective_source,
        site_ids=site_ids or None,
        keyword=q,
        has_content=has_content,
        agri_only=agri_only or bim_only,
        tag=tag,
        tags=tags or None,
    )
    return {
        "data_source": svc.data_source,
        **result,
    }


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    svc = get_data_service()
    stats_svc = get_stats_service()
    from src.core.site_sync import get_crawl_scope_label

    return _render(
        "stats.html",
        {
            "request": request,
            "data_source": svc.data_source,
            "active_nav": "stats",
            "stats": stats_svc.get_overview(),
            "crawl_scope_label": get_crawl_scope_label(),
        },
    )


@app.get("/api/stats/overview")
async def api_stats_overview():
    svc = get_data_service()
    stats_svc = get_stats_service()
    return {
        "data_source": svc.data_source,
        **stats_svc.get_overview(),
    }


_agri_sync_lock = asyncio.Lock()
_agri_sync_running = False
_agri_last_sync_summary: dict[str, Any] | None = None

_biz_clue_sync_running = False
_biz_clue_last_sync_summary: dict[str, Any] | None = None


async def _run_agri_sync_job(
    *,
    site_id: Optional[str] = None,
    max_items: Optional[int] = None,
) -> None:
    global _agri_sync_running, _agri_last_sync_summary
    from src.core.daily_agri_sync import run_daily_agri_sync
    from src.core.scheduler import load_yaml

    schedule_path = PROJECT_ROOT / "config" / "schedule.yaml"
    schedule_config = load_yaml(schedule_path) if schedule_path.exists() else {}
    try:
        summary = await run_daily_agri_sync(
            schedule_config=schedule_config,
            site_id=site_id,
            max_items=max_items,
            update_status=True,
        )
        _agri_last_sync_summary = summary
    except Exception as exc:
        logger.exception("农业同步失败: %s", exc)
        _agri_last_sync_summary = {"success": False, "error": str(exc)}
    finally:
        _agri_sync_running = False


async def _run_biz_clue_sync_job(
    *,
    site_id: Optional[str] = None,
    max_items: Optional[int] = None,
) -> None:
    global _biz_clue_sync_running, _biz_clue_last_sync_summary
    from src.core.biz_clue_sync import run_daily_biz_clue_sync
    from src.core.scheduler import load_yaml

    schedule_path = PROJECT_ROOT / "config" / "schedule.yaml"
    schedule_config = load_yaml(schedule_path) if schedule_path.exists() else {}
    try:
        summary = await run_daily_biz_clue_sync(
            schedule_config=schedule_config,
            site_id=site_id,
            max_items=max_items,
            update_status=True,
            trigger_analysis=True,
        )
        _biz_clue_last_sync_summary = summary
    except Exception as exc:
        logger.exception("商机同步失败: %s", exc)
        _biz_clue_last_sync_summary = {"success": False, "error": str(exc)}
    finally:
        _biz_clue_sync_running = False


@app.get("/insights", response_class=HTMLResponse)
async def insights_page(
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
    return _render(
        "insights.html",
        {
            "request": request,
            "data_source": svc.data_source,
            "sync_running": _biz_clue_sync_running,
            "active_nav": "insights",
            "days": days,
            "filter_stage": stage,
            "filter_product_line": product_line,
            "filter_level": level,
            "filtered_leads": leads_data.get("items") or [],
            **biz_ctx,
        },
    )


@app.get("/insights/config")
async def insights_config_redirect():
    """原商机配置管理入口，重定向至 SOP 向导。"""
    return RedirectResponse(url="/insights/sop", status_code=302)


@app.get("/insights/config/readonly", response_class=HTMLResponse)
async def insights_config_readonly_page(request: Request):
    from src.core.biz_clue_config import get_config_for_api

    svc = get_data_service()
    config = get_config_for_api()
    return _render(
        "insights_config.html",
        {
            "request": request,
            "data_source": svc.data_source,
            "active_nav": "insights",
            "config": config,
        },
    )


@app.get("/insights/sop", response_class=HTMLResponse)
async def insights_sop_page(request: Request):
    from src.web.biz_clue_sop_service import get_biz_clue_sop_service

    svc = get_data_service()
    domains = get_biz_clue_sop_service().list_domains()
    return _render(
        "insights_sop.html",
        {
            "request": request,
            "data_source": svc.data_source,
            "active_nav": "insights",
            "domains": domains.get("domains") or [],
        },
    )


@app.get("/insights/hermes", response_class=HTMLResponse)
async def insights_hermes_page(request: Request):
    from src.web.crawl_agent_routes import _default_chat_page_context

    svc = get_data_service()
    ctx = _default_chat_page_context(agent_profile="agri")
    ctx.update(
        {
            "request": request,
            "data_source": svc.data_source,
            "active_nav": "insights",
            "insights_home": "/insights",
        }
    )
    return _render("insights_hermes.html", ctx)


@app.get("/insights/tenders", response_class=HTMLResponse)
async def insights_tenders_page(
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
    return _render(
        "insights_tenders.html",
        {
            "request": request,
            "data_source": svc.data_source,
            "active_nav": "insights",
            "days": days,
            "tenders": tenders,
            "mvp_sites": agri_site_display_list(),
            "filters": tenders.get("filters") or {},
        },
    )


@app.get("/bim")
@app.get("/agri")
async def agri_legacy_redirect(request: Request):
    """兼容旧路径，重定向至主站商机洞察。"""
    target = "/insights"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(target, status_code=302)


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


@app.post("/api/agri/sync")
async def api_agri_sync(
    background_tasks: BackgroundTasks,
    site_id: Optional[str] = None,
    max_items: Optional[int] = None,
):
    global _agri_sync_running
    if _agri_sync_running:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": "农业同步任务已在运行",
                "running": True,
                "last_summary": _agri_last_sync_summary,
            },
        )
    _agri_sync_running = True
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
        "running": _agri_sync_running,
        "last_summary": _agri_last_sync_summary,
    }


@app.post("/api/biz-clue/sync")
async def api_biz_clue_sync(
    background_tasks: BackgroundTasks,
    site_id: Optional[str] = None,
    max_items: Optional[int] = None,
):
    global _biz_clue_sync_running
    if _biz_clue_sync_running:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": "商机同步任务已在运行",
                "running": True,
                "last_summary": _biz_clue_last_sync_summary,
            },
        )
    _biz_clue_sync_running = True
    background_tasks.add_task(
        _run_biz_clue_sync_job,
        site_id=(site_id or "").strip() or None,
        max_items=max_items,
    )
    return {
        "ok": True,
        "message": "商机同步已启动（后台执行）",
        "running": True,
    }


@app.get("/api/biz-clue/sync/status")
async def api_biz_clue_sync_status():
    return {
        "ok": True,
        "running": _biz_clue_sync_running,
        "last_summary": _biz_clue_last_sync_summary,
    }


@app.get("/api/biz-clue/config")
async def api_biz_clue_config():
    from src.core.biz_clue_config import get_config_for_api

    return {"ok": True, "config": get_config_for_api()}


@app.get("/api/biz-clue/config/product-lines")
async def api_biz_clue_config_product_lines():
    from src.core.biz_clue_config import get_product_lines

    return {"ok": True, "product_lines": get_product_lines()}


@app.get("/api/biz-clue/config/stages")
async def api_biz_clue_config_stages():
    from src.core.biz_clue_config import get_stages

    return {"ok": True, "stages": get_stages()}


class BizClueSopStartRequest(BaseModel):
    domain: str = ""
    custom_domain: str = ""
    use_llm: bool = False


class BizClueSopKeywordsRequest(BaseModel):
    search_keywords: Optional[list[str]] = None
    exclude_keywords: Optional[list[str]] = None
    regenerate: bool = False
    use_llm: bool = False


class BizClueSopSitesRequest(BaseModel):
    selected_site_ids: Optional[list[str]] = None
    rematch: bool = False
    limit: int = 30


class BizClueSopTasksRequest(BaseModel):
    cron: Optional[str] = None
    apply_sources: bool = True
    create_incremental_tasks: bool = True


class BizClueSopNotifyRequest(BaseModel):
    cron: Optional[str] = None
    task_name: Optional[str] = None
    feishu_push: bool = True
    feishu_webhook_url: Optional[str] = None


@app.get("/api/biz-clue/sop/domains")
async def api_biz_clue_sop_domains():
    from src.web.biz_clue_sop_service import get_biz_clue_sop_service

    return {"ok": True, **get_biz_clue_sop_service().list_domains()}


@app.post("/api/biz-clue/sop/start")
async def api_biz_clue_sop_start(body: BizClueSopStartRequest):
    from src.web.biz_clue_sop_service import get_biz_clue_sop_service

    result = get_biz_clue_sop_service().start_session(
        domain=body.domain.strip(),
        custom_domain=body.custom_domain.strip(),
        use_llm=body.use_llm,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "创建 SOP 会话失败")
    return result


@app.get("/api/biz-clue/sop/{session_id}")
async def api_biz_clue_sop_get(session_id: str):
    from src.web.biz_clue_sop_service import get_biz_clue_sop_service

    result = get_biz_clue_sop_service().get_session(session_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error") or "会话不存在")
    return result


@app.post("/api/biz-clue/sop/{session_id}/keywords")
async def api_biz_clue_sop_keywords(session_id: str, body: BizClueSopKeywordsRequest):
    from src.web.biz_clue_sop_service import get_biz_clue_sop_service

    result = get_biz_clue_sop_service().save_keywords(
        session_id,
        search_keywords=body.search_keywords,
        exclude_keywords=body.exclude_keywords,
        regenerate=body.regenerate,
        use_llm=body.use_llm,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "保存关键词失败")
    return result


@app.post("/api/biz-clue/sop/{session_id}/sites")
async def api_biz_clue_sop_sites(session_id: str, body: BizClueSopSitesRequest):
    from src.web.biz_clue_sop_service import get_biz_clue_sop_service

    result = get_biz_clue_sop_service().match_and_save_sites(
        session_id,
        selected_site_ids=body.selected_site_ids,
        rematch=body.rematch,
        limit=body.limit,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "站点匹配失败")
    return result


@app.post("/api/biz-clue/sop/{session_id}/tasks")
async def api_biz_clue_sop_tasks(session_id: str, body: BizClueSopTasksRequest):
    from src.web.biz_clue_sop_service import get_biz_clue_sop_service

    result = get_biz_clue_sop_service().create_crawl_tasks(
        session_id,
        cron=body.cron,
        apply_sources=body.apply_sources,
        create_incremental_tasks=body.create_incremental_tasks,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "创建爬取任务失败")
    return result


@app.post("/api/biz-clue/sop/{session_id}/notify")
async def api_biz_clue_sop_notify(session_id: str, body: BizClueSopNotifyRequest):
    from src.web.biz_clue_sop_service import get_biz_clue_sop_service

    result = get_biz_clue_sop_service().configure_notify(
        session_id,
        cron=body.cron,
        task_name=body.task_name,
        feishu_push=body.feishu_push,
        feishu_webhook_url=(body.feishu_webhook_url or "").strip() or None,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "配置推送失败")
    return result


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


@app.get("/api/bim/latest")
@app.get("/api/agri/latest")
async def api_agri_latest(
    limit: int = Query(20, ge=1, le=100),
    days: int = Query(DEFAULT_AGRI_DAYS, ge=1, le=MAX_AGRI_DAYS),
    site_id: Optional[str] = None,
):
    svc = get_data_service()
    result = get_agri_insights_service().get_latest(limit=limit, days=days, site_id=site_id)
    return {"data_source": svc.data_source, **result}


@app.get("/api/bim/insights")
@app.get("/api/agri/insights")
async def api_agri_insights(
    days: int = Query(DEFAULT_AGRI_DAYS, ge=1, le=MAX_AGRI_DAYS),
    site_id: Optional[str] = None,
):
    svc = get_data_service()
    result = get_agri_insights_service().get_insights(
        days=days,
        site_id=site_id,
    )
    return {"data_source": svc.data_source, **result}


@app.get("/api/notices/{notice_id}")
async def api_notice_detail(notice_id: str):
    svc = get_data_service()
    notice = svc.get_notice(notice_id)
    if not notice:
        raise HTTPException(status_code=404, detail="公告不存在")
    return {"data_source": svc.data_source, "notice": notice}


class NoticeTagsUpdateRequest(BaseModel):
    tags: list[str] = []


class SettingsUpdateRequest(BaseModel):
    hermes_agent_url: Optional[str] = None
    hermes_agent_chat_url: Optional[str] = None
    hermes_agent_api_key: Optional[str] = None
    llm_model: Optional[str] = None
    llm_api_key: Optional[str] = None


@app.get("/api/settings")
async def api_get_settings():
    """返回当前系统配置（API key 打码）。"""
    return get_public_settings()


@app.put("/api/settings")
async def api_put_settings(body: SettingsUpdateRequest):
    """保存系统配置到 .env。"""
    save_settings(body.model_dump(exclude_unset=True))
    return {"ok": True}


@app.get("/api/tags")
async def api_list_notice_tags():
    svc = get_data_service()
    return {"data_source": svc.data_source, "tags": svc.list_tags()}


@app.patch("/api/notices/{notice_id}/tags")
async def api_update_notice_tags(notice_id: str, body: NoticeTagsUpdateRequest):
    svc = get_data_service()
    notice = svc.update_notice_tags(notice_id, body.tags)
    if not notice:
        raise HTTPException(status_code=404, detail="公告不存在或无法更新")
    return {
        "data_source": svc.data_source,
        "notice": notice,
        "tags": notice.get("tags") or [],
    }


def _tasks_page_context(request: Request) -> dict[str, Any]:
    svc = get_data_service()
    task_svc = get_task_service()
    data = task_svc.list_sites()
    from src.core.crawl_agent_actions import enrich_action, list_actions

    recent_agent_actions = [enrich_action(r) for r in list_actions(limit=20)]

    return {
        "request": request,
        "data_source": svc.data_source,
        "backend": data["backend"],
        "daily_sync": data["daily_sync"],
        "crawl_schedule": data["crawl_schedule"],
        "summary": data["summary"],
        "sites": data["sites"],
        "recent_agent_actions": recent_agent_actions,
        "active_nav": "tasks",
    }


def _render_run_detail(request: Request, run_id: str) -> HTMLResponse:
    svc = get_data_service()
    sched_svc = get_schedule_service()
    detail = sched_svc.get_run(run_id)
    if not detail:
        return _render(
            "run_not_found.html",
            {
                "request": request,
                "data_source": svc.data_source,
                "run_id": run_id,
            },
            status_code=404,
        )
    return _render(
        "schedule_run.html",
        {
            "request": request,
            "data_source": svc.data_source,
            "run": detail["run"],
            "events": detail["events"],
            "log_lines": detail["log_lines"],
            "crawled_links": detail.get("crawled_links", []),
            "link_count": detail.get("link_count", 0),
        },
    )


@app.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request):
    return _render("tasks.html", _tasks_page_context(request))


@app.get("/sites", response_class=HTMLResponse)
async def sites_page(request: Request, all_sites: bool = Query(False, alias="all")):
    svc = get_data_service()
    sites_svc = get_sites_service()
    data = sites_svc.list_sites(include_all=all_sites)
    return _render(
        "sites.html",
        {
            "request": request,
            "data_source": svc.data_source,
            "summary": data["summary"],
            "sites": data["sites"],
            "active_nav": "sites",
        },
    )


@app.get("/sites/configure-client", response_class=HTMLResponse)
async def configure_client_page(request: Request, site_id: str = Query(default="")):
    svc = get_data_service()
    return _render(
        "configure_client.html",
        {
            "request": request,
            "data_source": svc.data_source,
            "site_id": site_id.strip(),
            "active_nav": "sites",
        },
    )


@app.get("/api/sites")
async def api_sites(all_sites: bool = Query(False, alias="all")):
    return get_sites_service().list_sites(include_all=all_sites)


@app.delete("/api/sites/{site_id}")
async def api_delete_site(site_id: str):
    result = get_sites_service().delete_site(site_id)
    if not result.get("ok"):
        error = result.get("error") or "删除失败"
        if "受保护" in error:
            raise HTTPException(status_code=403, detail=error)
        if "不存在" in error:
            raise HTTPException(status_code=404, detail=error)
        raise HTTPException(status_code=400, detail=error)
    return result


@app.get("/tasks/runs/{run_id}", response_class=HTMLResponse)
async def tasks_run_page(request: Request, run_id: str):
    return _render_run_detail(request, run_id)


@app.get("/tasks/{site_id}", response_class=HTMLResponse)
async def tasks_site_page(request: Request, site_id: str):
    svc = get_data_service()
    task_svc = get_task_service()
    detail = task_svc.get_site(site_id)
    if not detail:
        raise HTTPException(status_code=404, detail="站点不存在或未启用")
    return _render(
        "tasks_site.html",
        {
            "request": request,
            "data_source": svc.data_source,
            "site": detail["site"],
            "runs": detail["runs"],
        },
    )


@app.get("/api/tasks/sites")
async def api_tasks_sites():
    task_svc = get_task_service()
    return task_svc.list_sites()


@app.get("/api/tasks/sites/{site_id}")
async def api_tasks_site_detail(site_id: str, runs_limit: int = Query(20, ge=1, le=100)):
    task_svc = get_task_service()
    detail = task_svc.get_site(site_id, runs_limit=runs_limit)
    if not detail:
        raise HTTPException(status_code=404, detail="站点不存在或未启用")
    return detail


@app.get("/sync")
async def sync_status_redirect():
    return RedirectResponse(url="/tasks", status_code=302)


@app.get("/api/sync/status")
async def api_sync_status(include_bim_totals: bool = Query(False)):
    sync_svc = get_sync_service()
    return sync_svc.list_status(include_bim_totals=include_bim_totals)


@app.get("/api/sync/{site_id}/status")
async def api_sync_site_status(site_id: str):
    sync_svc = get_sync_service()
    result = sync_svc.get_sync_status(site_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message", "站点不存在"))
    return result


@app.post("/api/sync/{site_id}/trigger")
async def api_trigger_sync(site_id: str):
    sync_svc = get_sync_service()
    result = await sync_svc.trigger_sync(site_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message", "无法触发同步"))
    return result


@app.post("/api/sync/{site_id}/retry")
async def api_retry_sync(site_id: str):
    sync_svc = get_sync_service()
    result = await sync_svc.retry_sync(site_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message", "无法重试同步"))
    return result


@app.post("/api/sync/{site_id}/cancel")
async def api_cancel_sync(site_id: str):
    sync_svc = get_sync_service()
    result = await sync_svc.cancel_sync(site_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message", "无法中止同步"))
    return result


@app.post("/api/sync/{site_id}/reset")
async def api_reset_sync(site_id: str):
    sync_svc = get_sync_service()
    result = sync_svc.reset_sync_status(site_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message", "无法重置同步状态"))
    return result


# ── 站点凭据 API ──────────────────────────────────────────────


class SiteCredentialsRequest(BaseModel):
    credential_type: Literal["cookie", "headers"] = "cookie"
    cookie_header: Optional[str] = None
    cookies: Optional[list[dict[str, Any]]] = None
    headers: Optional[dict[str, str]] = None
    notes: Optional[str] = None


@app.post("/api/sites/{site_id}/credentials")
async def api_save_site_credentials(site_id: str, body: SiteCredentialsRequest):
    from src.core.site_credentials import save_site_credentials

    site = get_site_by_id(site_id)
    if not site:
        raise HTTPException(status_code=404, detail="站点不存在")
    try:
        return save_site_credentials(
            site_id,
            site_url=site.get("url", ""),
            credential_type=body.credential_type,
            cookie_header=body.cookie_header,
            cookies=body.cookies,
            headers=body.headers,
            notes=body.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/sites/{site_id}/credentials")
async def api_get_site_credentials(site_id: str):
    from src.core.site_credentials import get_credentials_metadata

    site = get_site_by_id(site_id)
    if not site:
        raise HTTPException(status_code=404, detail="站点不存在")
    return get_credentials_metadata(site_id)


@app.delete("/api/sites/{site_id}/credentials")
async def api_delete_site_credentials(site_id: str):
    from src.core.site_credentials import delete_site_credentials

    site = get_site_by_id(site_id)
    if not site:
        raise HTTPException(status_code=404, detail="站点不存在")
    result = delete_site_credentials(site_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message", "凭据不存在"))
    return result


# ── 定时爬取任务 API ──────────────────────────────────────────


@app.get("/schedules")
async def schedules_redirect():
    return RedirectResponse(url="/tasks", status_code=302)


@app.get("/schedules/runs/{run_id}", response_class=HTMLResponse)
async def schedule_run_page(request: Request, run_id: str):
    return _render_run_detail(request, run_id)


@app.get("/api/schedules/jobs")
async def api_schedule_jobs():
    sched_svc = get_schedule_service()
    return sched_svc.list_jobs()


@app.get("/api/schedules/runs")
async def api_schedule_runs(
    site_id: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    status: Optional[str] = None,
):
    sched_svc = get_schedule_service()
    return sched_svc.list_runs(site_id=site_id, limit=limit, status=status)


@app.get("/api/schedules/runs/{run_id}")
async def api_schedule_run_detail(run_id: str):
    sched_svc = get_schedule_service()
    detail = sched_svc.get_run(run_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Run 不存在")
    return detail


@app.post("/api/schedules/runs/{run_id}/retry")
async def api_retry_schedule_run(run_id: str):
    sched_svc = get_schedule_service()
    result = await sched_svc.retry_run(run_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message", "无法重试"))
    return result


@app.post("/api/schedules/runs/{run_id}/cancel")
async def api_cancel_schedule_run(run_id: str):
    sync_svc = get_sync_service()
    result = await sync_svc.cancel_run(run_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message", "无法中止"))
    return result


@app.post("/api/schedules/jobs/{site_id}/retry")
async def api_retry_schedule_job(site_id: str):
    sched_svc = get_schedule_service()
    result = await sched_svc.retry_job(site_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message", "无法重试"))
    return result


# ── scripts/ Cron 批量入库 API ──────────────────────────────────


class ScriptCronCreateRequest(BaseModel):
    script: str
    cron: str
    args: Optional[list[str] | dict[str, Any]] = None
    name: Optional[str] = None
    enabled: bool = True
    job_id: Optional[str] = None


class ScriptCronUpdateRequest(BaseModel):
    script: Optional[str] = None
    cron: Optional[str] = None
    args: Optional[list[str] | dict[str, Any]] = None
    name: Optional[str] = None
    enabled: Optional[bool] = None


@app.get("/api/script-cron/jobs")
async def api_script_cron_jobs():
    return get_script_cron_service().list_jobs()


@app.get("/api/script-cron/jobs/{job_id}")
async def api_script_cron_job_detail(job_id: str):
    result = get_script_cron_service().get_job(job_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "任务不存在"))
    return result


@app.post("/api/script-cron/jobs")
async def api_script_cron_create(body: ScriptCronCreateRequest):
    result = get_script_cron_service().create_job(
        script=body.script,
        cron=body.cron,
        args=body.args,
        name=body.name,
        enabled=body.enabled,
        job_id=body.job_id,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "创建失败"))
    return result


@app.put("/api/script-cron/jobs/{job_id}")
async def api_script_cron_update(job_id: str, body: ScriptCronUpdateRequest):
    fields = body.model_dump(exclude_unset=True)
    result = get_script_cron_service().update_job(job_id, **fields)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "更新失败"))
    return result


@app.delete("/api/script-cron/jobs/{job_id}")
async def api_script_cron_delete(job_id: str):
    result = get_script_cron_service().delete_job(job_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "删除失败"))
    return result


@app.post("/api/script-cron/jobs/{job_id}/run")
async def api_script_cron_run(job_id: str):
    result = get_script_cron_service().run_now(job_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error") or result.get("message"))
    return result


# ── Agent 智能采集 API ──────────────────────────────────────────


class CreateAgentTaskRequest(BaseModel):
    site_id: Optional[str] = None
    url: Optional[str] = None
    max_items: int = 10


@app.get("/agent/logs", response_class=HTMLResponse)
async def agent_logs_page(request: Request):
    """Agent 文件日志页面（与 /agent 共用模板，默认打开文件日志 Tab）。"""
    svc = get_data_service()
    mgr = get_task_manager()
    tasks = mgr.list_tasks(limit=10)
    sites = mgr.list_sites()
    settings = get_settings()
    crawl_logger = get_agent_crawl_logger()
    return _render(
        "agent.html",
        {
            "request": request,
            "data_source": svc.data_source,
            "sites": sites,
            "tasks": [t.to_dict() for t in tasks],
            "llm_configured": bool(settings.openai_api_key),
            "llm_model": settings.llm_model,
            "default_tab": "file_logs",
            "log_file_path": str(crawl_logger.main_log_path),
        },
    )


@app.get("/agent", response_class=HTMLResponse)
async def agent_page(request: Request):
    svc = get_data_service()
    mgr = get_task_manager()
    tasks = mgr.list_tasks(limit=10)
    sites = mgr.list_sites()
    settings = get_settings()
    crawl_logger = get_agent_crawl_logger()
    return _render(
        "agent.html",
        {
            "request": request,
            "data_source": svc.data_source,
            "sites": sites,
            "tasks": [t.to_dict() for t in tasks],
            "llm_configured": bool(settings.openai_api_key),
            "llm_model": settings.llm_model,
            "default_tab": "live",
            "log_file_path": str(crawl_logger.main_log_path),
        },
    )


@app.post("/api/agent/tasks")
async def api_create_agent_task(body: CreateAgentTaskRequest):
    if not body.site_id and not body.url:
        raise HTTPException(status_code=400, detail="需提供 site_id 或 url")
    mgr = get_task_manager()
    try:
        task = await mgr.create_task(
            site_id=body.site_id,
            url=body.url,
            max_items=body.max_items,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "task": task.to_dict()}


@app.get("/api/agent/tasks")
async def api_list_agent_tasks(limit: int = Query(20, ge=1, le=100)):
    mgr = get_task_manager()
    tasks = mgr.list_tasks(limit=limit)
    return {"tasks": [t.to_dict() for t in tasks]}


@app.get("/api/agent/tasks/{task_id}")
async def api_get_agent_task(task_id: str):
    mgr = get_task_manager()
    task = mgr.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"task": task.to_dict()}


@app.get("/api/agent/tasks/{task_id}/stream")
async def api_stream_agent_task(task_id: str):
    mgr = get_task_manager()
    task = mgr.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    from src.web.sse_utils import SSE_STREAM_HEADERS

    async def event_generator():
        async for event in mgr.stream_events(task_id):
            payload = json.dumps(event.to_sse_dict(), ensure_ascii=False)
            yield f"data: {payload}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=SSE_STREAM_HEADERS,
    )


@app.post("/api/agent/tasks/{task_id}/cancel")
async def api_cancel_agent_task(task_id: str):
    mgr = get_task_manager()
    result = await mgr.cancel_task(task_id)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message", "无法取消"))
    return result


# ── 爬取规则 AI 生成 API ──────────────────────────────────────


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"] = "user"
    content: str


class GenerateRuleRequest(BaseModel):
    site_id: str
    url: str
    messages: list[ChatMessage] = []
    save: bool = False


class SaveRuleRequest(BaseModel):
    site_id: str
    yaml: str


class ValidateRuleRequest(BaseModel):
    site_id: Optional[str] = None
    yaml: str


class DryRunRuleRequest(BaseModel):
    site_id: str
    yaml: Optional[str] = None
    max_items: int = 20
    max_pages: int = 1


class GenerateFromRecordingRequest(BaseModel):
    site_id: str
    url: str = ""
    recording: dict[str, Any]
    save: bool = False
    prefer_llm: bool = True


@app.get("/crawl-rules/{site_id}/workflow", response_class=HTMLResponse)
async def crawl_rules_workflow_page(request: Request, site_id: str):
    from src.web.crawl_rule_workflow_service import get_crawl_rule_workflow_service

    site = get_site_by_id(site_id)
    if not site:
        raise HTTPException(status_code=404, detail="站点不存在")
    svc = get_data_service()
    wf_svc = get_crawl_rule_workflow_service()
    data = wf_svc.get_workflow(site_id)
    return _render(
        "crawl_rules_workflow.html",
        {
            "request": request,
            "data_source": svc.data_source,
            "site_id": site_id,
            "site_name": site.get("name", site_id),
            "site_url": site.get("url", ""),
            "active_nav": "sites",
        },
    )


@app.get("/api/crawl-rules/{site_id}/workflow")
async def api_get_crawl_rule_workflow(site_id: str):
    from src.web.crawl_rule_workflow_service import get_crawl_rule_workflow_service

    result = get_crawl_rule_workflow_service().get_workflow(site_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "站点不存在"))
    return result


class WorkflowUpdateRequest(BaseModel):
    nodes: list[dict[str, Any]]
    edges: list[dict[str, str]] = []


@app.put("/api/crawl-rules/{site_id}/workflow")
async def api_put_crawl_rule_workflow(site_id: str, body: WorkflowUpdateRequest):
    from src.web.crawl_rule_workflow_service import get_crawl_rule_workflow_service

    result = get_crawl_rule_workflow_service().save_workflow(
        site_id, body.model_dump(mode="json")
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=400,
            detail={"errors": result.get("errors") or [result.get("error", "保存失败")]},
        )
    return result


class WorkflowGenerateRequest(BaseModel):
    url: str = ""
    prompt: str = ""
    page_hints: str = ""
    save: bool = False


@app.post("/api/crawl-rules/{site_id}/workflow/generate")
async def api_generate_crawl_rule_workflow(site_id: str, body: WorkflowGenerateRequest = Body(default_factory=WorkflowGenerateRequest)):
    from src.web.crawl_rule_workflow_service import get_crawl_rule_workflow_service

    result = await get_crawl_rule_workflow_service().generate_workflow(
        site_id,
        url=body.url,
        prompt=body.prompt,
        page_hints=body.page_hints,
        save=body.save,
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=400,
            detail={"errors": result.get("errors") or [result.get("error", "生成失败")]},
        )
    return result


class SiteDryRunRequest(BaseModel):
    max_items: int = 5
    max_pages: int = 1


@app.post("/api/crawl-rules/{site_id}/dry-run")
async def api_dry_run_crawl_rule_by_site(site_id: str, body: SiteDryRunRequest = Body(default_factory=SiteDryRunRequest)):
    from src.core.crawl_rule_dry_run import dry_run_crawl_rule

    return await dry_run_crawl_rule(
        site_id,
        max_items=body.max_items,
        max_pages=body.max_pages,
    )


@app.get("/crawl-rules/{site_id}", response_class=HTMLResponse)
async def crawl_rules_page(request: Request, site_id: str):
    site = get_site_by_id(site_id)
    if not site:
        raise HTTPException(status_code=404, detail="站点不存在")
    svc = get_data_service()
    settings = get_settings()
    existing = load_rule_yaml(site_id) or ""
    return _render(
        "crawl_rules.html",
        {
            "request": request,
            "data_source": svc.data_source,
            "site_id": site_id,
            "site_name": site.get("name", site_id),
            "site_url": site.get("url", ""),
            "existing_yaml": existing,
            "llm_configured": bool(settings.openai_api_key),
            "llm_model": settings.llm_model,
        },
    )


@app.get("/api/crawl-rules/{site_id}")
async def api_get_crawl_rule(site_id: str):
    yaml_text = load_rule_yaml(site_id)
    if yaml_text is None:
        raise HTTPException(status_code=404, detail="规则文件不存在")
    result = validate_yaml(yaml_text, site_id=site_id)
    return {"site_id": site_id, "yaml": yaml_text, **result}


@app.post("/api/crawl-rules/generate")
async def api_generate_crawl_rule(body: GenerateRuleRequest):
    gen = get_rule_generator()
    messages = [{"role": m.role, "content": m.content} for m in body.messages]
    result = await gen.generate(
        site_id=body.site_id,
        url=body.url,
        messages=messages,
    )
    if body.save and result.get("valid") and result.get("yaml"):
        save_result = save_rule_yaml(body.site_id, result["yaml"])
        result["saved"] = save_result.get("ok", False)
        if save_result.get("path"):
            result["path"] = save_result["path"]
        if not save_result.get("ok"):
            result["save_errors"] = save_result.get("errors", [])
    return result


@app.post("/api/crawl-rules/generate-from-recording")
async def api_generate_from_recording(body: GenerateFromRecordingRequest):
    gen = get_rule_generator()
    result = await gen.generate_from_recording(
        site_id=body.site_id,
        url=body.url,
        recording=body.recording,
        prefer_llm=body.prefer_llm,
    )
    if body.save and result.get("valid") and result.get("yaml"):
        save_result = save_rule_yaml(body.site_id, result["yaml"])
        result["saved"] = save_result.get("ok", False)
        if save_result.get("path"):
            result["path"] = save_result["path"]
        if not save_result.get("ok"):
            result["save_errors"] = save_result.get("errors", [])
    return result


@app.post("/api/crawl-rules/save")
async def api_save_crawl_rule(body: SaveRuleRequest):
    result = save_rule_yaml(body.site_id, body.yaml)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail={"errors": result.get("errors", [])})
    return result


@app.post("/api/crawl-rules/validate")
async def api_validate_crawl_rule(body: ValidateRuleRequest):
    return validate_yaml(body.yaml, site_id=body.site_id)


@app.post("/api/crawl-rules/dry-run")
async def api_dry_run_crawl_rule(body: DryRunRuleRequest):
    from src.core.crawl_rule_dry_run import dry_run_crawl_rule

    return await dry_run_crawl_rule(
        body.site_id,
        yaml_text=body.yaml,
        max_items=body.max_items,
        max_pages=body.max_pages,
    )


class GenerateScriptRequest(BaseModel):
    site_id: str
    url: str = ""
    messages: list[ChatMessage] = []
    save: bool = False
    dry_run: bool = False
    max_items: int = 5


class RunScriptRequest(BaseModel):
    site_id: str
    dry_run: bool = False
    max_items: Optional[int] = None
    persist: bool = True


@app.post("/api/crawl-scripts/generate")
async def api_generate_crawl_script(body: GenerateScriptRequest):
    from src.core.crawl_script_service import generate_crawl_script

    messages = [{"role": m.role, "content": m.content} for m in body.messages]
    return await generate_crawl_script(
        site_id=body.site_id,
        url=body.url,
        messages=messages,
        save=body.save,
        dry_run=body.dry_run,
        max_items=body.max_items,
    )


@app.post("/api/crawl-scripts/run")
async def api_run_crawl_script(body: RunScriptRequest):
    from src.core.crawl_script_service import run_generated_crawl

    return await run_generated_crawl(
        body.site_id,
        dry_run=body.dry_run,
        max_items=body.max_items,
        persist=body.persist,
        trigger="manual",
    )


@app.get("/api/crawl-scripts/{site_id}")
async def api_get_crawl_script_status(site_id: str):
    from src.core.crawl_script_service import get_crawl_script_status

    result = get_crawl_script_status(site_id.strip())
    if result.get("site") is None and not result.get("metadata") and not result.get("has_rule_file"):
        raise HTTPException(status_code=404, detail=f"站点或脚本不存在: {site_id}")
    return result


# ── Crawl Agent / Hermes 对话（共享路由模块）──────────────────────


def _format_pending_for_ui(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    formatted: list[dict[str, Any]] = []
    for item in items:
        row = dict(item)
        created = item.get("created_at") or ""
        if created and "T" in created:
            try:
                row["created_at_fmt"] = created.replace("T", " ").split("+")[0].split(".")[0]
            except (ValueError, IndexError):
                row["created_at_fmt"] = created
        else:
            row["created_at_fmt"] = created or "—"
        formatted.append(row)
    return formatted


from src.web.crawl_agent_routes import register_crawl_agent_routes

register_crawl_agent_routes(
    app,
    render=_render,
    format_pending_for_ui=_format_pending_for_ui,
)


class BrowserNavigateRequest(BaseModel):
    url: str
    session_id: Optional[str] = None
    new_tab: bool = True


class BrowserSessionRequest(BaseModel):
    session_id: Optional[str] = None


class BrowserGoRequest(BaseModel):
    direction: Literal["back", "forward"]
    session_id: Optional[str] = None


class BrowserScreenshotRequest(BaseModel):
    session_id: Optional[str] = None
    format: Literal["png", "jpeg"] = "png"
    quality: int = 80
    selector: Optional[str] = None


class BrowserCrawlRequest(BaseModel):
    session_id: Optional[str] = None
    site_id: Optional[str] = None
    max_pages: Optional[int] = None
    save: bool = True
    confirm_generic: bool = False


class BrowserInferSiteRequest(BaseModel):
    url: str


@app.get("/browser", response_class=HTMLResponse)
async def browser_control_page(request: Request):
    svc = get_data_service()
    return _render(
        "browser.html",
        {
            "request": request,
            "data_source": svc.data_source,
            "active_nav": "browser",
        },
    )


@app.get("/api/browser/status")
async def api_browser_status():
    from src.web.webbridge_skills import check_webbridge

    result = check_webbridge()
    return {"ok": result.get("ok"), "result": result}


@app.post("/api/browser/navigate")
async def api_browser_navigate(body: BrowserNavigateRequest):
    from src.web.webbridge_skills import webbridge_navigate

    url = (body.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url 不能为空")
    result = webbridge_navigate(
        url,
        session_id=(body.session_id or "").strip() or None,
        new_tab=body.new_tab,
    )
    return {"ok": result.get("ok") is not False, "result": result, "error": result.get("error")}


@app.post("/api/browser/snapshot")
async def api_browser_snapshot(body: BrowserSessionRequest = Body(default_factory=BrowserSessionRequest)):
    from src.web.webbridge_skills import webbridge_snapshot

    result = webbridge_snapshot(session_id=(body.session_id or "").strip() or None)
    return {"ok": result.get("ok") is not False, "result": result, "error": result.get("error")}


@app.post("/api/browser/go")
async def api_browser_go(body: BrowserGoRequest):
    from src.web.webbridge_skills import webbridge_go

    direction = (body.direction or "").strip().lower()
    if direction not in ("back", "forward"):
        raise HTTPException(status_code=400, detail="direction 须为 back 或 forward")
    result = webbridge_go(
        direction,
        session_id=(body.session_id or "").strip() or None,
    )
    return {"ok": result.get("ok") is not False, "result": result, "error": result.get("error")}


@app.post("/api/browser/screenshot")
async def api_browser_screenshot(body: BrowserScreenshotRequest = Body(default_factory=BrowserScreenshotRequest)):
    from src.web.webbridge_skills import webbridge_screenshot

    result = webbridge_screenshot(
        session_id=(body.session_id or "").strip() or None,
        format=body.format,
        quality=body.quality,
        selector=(body.selector or "").strip() or None,
    )
    return {"ok": result.get("ok") is not False, "result": result, "error": result.get("error")}


@app.get("/api/browser/screenshot/{filename}")
async def api_browser_screenshot_file(filename: str):
    from fastapi.responses import FileResponse
    from src.web.webbridge_skills import SCREENSHOT_DIR

    safe_name = Path(filename).name
    if safe_name != filename or not safe_name:
        raise HTTPException(status_code=400, detail="非法文件名")
    path = SCREENSHOT_DIR / safe_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="截图不存在")
    media = "image/jpeg" if safe_name.lower().endswith(".jpg") else "image/png"
    return FileResponse(path, media_type=media)


@app.post("/api/browser/infer-site")
async def api_browser_infer_site(body: BrowserInferSiteRequest):
    from src.web.browser_crawl import infer_site_id_from_url
    from src.web.crawl_agent_tools import resolve_site_id

    url = (body.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url 不能为空")
    site_id = resolve_site_id(url) or infer_site_id_from_url(url)
    site = get_site_by_id(site_id) if site_id else None
    return {
        "ok": True,
        "site_id": site_id,
        "site_name": (site or {}).get("name") if site else None,
        "registered": site is not None,
    }


class SiteRegisterRequest(BaseModel):
    name: str
    url: str


class BimAnalysisScheduleRequest(BaseModel):
    name: str = "商机每日综合分析"
    cron: Optional[str] = None
    cron_expression: Optional[str] = None
    cron_preset: Optional[str] = None
    cron_custom: Optional[str] = None
    feishu_webhook_url: Optional[str] = None
    top_n: int = 10
    enabled: bool = True


class ScheduledTaskCreateRequest(BaseModel):
    task_type: str = "hermes_summary"
    name: str
    cron: Optional[str] = None
    cron_expression: Optional[str] = None
    cron_preset: Optional[str] = None
    cron_custom: Optional[str] = None
    hermes_prompt: Optional[str] = None
    prompt: Optional[str] = None
    push_mode: str = "excerpt"
    push_excerpt_max: int = 1500
    feishu_webhook_url: Optional[str] = None
    feishu_webhooks: Optional[list[Any] | str] = None
    top_n: int = 10
    days: int = 1
    report_kind: Optional[str] = None
    push_target: Optional[str] = None
    feishu_user_ids: Optional[list[str] | str] = None
    notification_channels: Optional[list[str] | str] = None
    channel_config: Optional[dict[str, Any]] = None
    enabled: bool = True
    feishu_push: bool = True


class ScheduledTaskPatchRequest(BaseModel):
    enabled: Optional[bool] = None
    task_type: Optional[str] = None
    cron: Optional[str] = None
    cron_expression: Optional[str] = None
    cron_preset: Optional[str] = None
    cron_custom: Optional[str] = None
    name: Optional[str] = None
    hermes_prompt: Optional[str] = None
    prompt: Optional[str] = None
    push_mode: Optional[str] = None
    push_excerpt_max: Optional[int] = None
    feishu_push: Optional[bool] = None
    top_n: Optional[int] = None
    days: Optional[int] = None
    report_kind: Optional[str] = None
    feishu_user_ids: Optional[list[str] | str] = None
    feishu_webhook_url: Optional[str] = None
    feishu_webhooks: Optional[list[Any] | str] = None
    notification_channels: Optional[list[str] | str] = None
    channel_config: Optional[dict[str, Any]] = None


class ScheduledTaskRunRequest(BaseModel):
    dry_run: bool = False


class ScheduledTaskToggleRequest(BaseModel):
    enabled: Optional[bool] = None


@app.post("/api/sites/register")
async def api_sites_register(body: SiteRegisterRequest):
    from src.core.site_onboarding import register_site

    try:
        status = register_site(body.name, body.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **status}


def _resolve_cron_from_request(
    cron: Optional[str] = None,
    cron_expression: Optional[str] = None,
    cron_preset: Optional[str] = None,
    cron_custom: Optional[str] = None,
) -> str:
    from src.core.chat_scheduled_tasks import _normalize_cron_with_extras

    cron_raw = (cron_expression or cron or "").strip()
    if (cron_preset or "").strip() == "custom":
        cron_raw = (cron_custom or cron_raw).strip()
    elif not cron_raw and (cron_preset or "").strip():
        cron_raw = (cron_preset or "").strip()
    if not cron_raw:
        raise HTTPException(status_code=400, detail="cron 不能为空")
    try:
        return _normalize_cron_with_extras(cron_raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/scheduled-tasks", response_class=HTMLResponse)
async def scheduled_tasks_page(request: Request):
    from src.core.chat_scheduled_tasks import PROMPT_PRESETS, PUSH_MODES, REPORT_KINDS, TASK_TYPE_LABELS
    from src.core.feishu_push import is_feishu_push_configured
    from src.web.chat_schedule_service import get_chat_schedule_service

    meta = get_chat_schedule_service().get_meta()
    return _render(
        "scheduled_tasks.html",
        {
            "request": request,
            "active_nav": "scheduled_tasks",
            "task_type_labels": TASK_TYPE_LABELS,
            "cron_presets": meta.get("cron_presets") or [],
            "prompt_presets": PROMPT_PRESETS,
            "push_modes": PUSH_MODES,
            "report_kinds": REPORT_KINDS,
            "feishu_configured": is_feishu_push_configured("webhook"),
        },
    )


@app.get("/api/chat/scheduled-tasks/meta")
async def api_scheduled_tasks_meta():
    from src.web.chat_schedule_service import get_chat_schedule_service

    return get_chat_schedule_service().get_meta()


@app.post("/api/chat/scheduled-tasks")
async def api_create_scheduled_task(body: ScheduledTaskCreateRequest):
    from src.core.chat_scheduled_tasks import (
        TASK_TYPE_BIM_DAILY_ANALYSIS,
        TASK_TYPE_BIM_DAILY_BRIEF,
        TASK_TYPE_BIM_WEEKLY_BRIEF,
        TASK_TYPE_ENGINEERING_LLM_REPORT,
        is_hermes_task_type,
    )
    from src.web.chat_schedule_service import get_chat_schedule_service

    cron_expr = _resolve_cron_from_request(
        body.cron,
        body.cron_expression,
        body.cron_preset,
        body.cron_custom,
    )
    svc = get_chat_schedule_service()
    task_type = (body.task_type or "hermes_summary").strip()

    if is_hermes_task_type(task_type):
        prompt = (body.hermes_prompt or body.prompt or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="Hermes 任务需要填写提示词")
        result = svc.create_hermes_summary_task(
            cron=cron_expr,
            name=(body.name or "").strip(),
            hermes_prompt=prompt,
            push_mode=(body.push_mode or "excerpt").strip(),
            push_excerpt_max=max(200, min(int(body.push_excerpt_max or 1500), 4000)),
            feishu_webhook_url=(body.feishu_webhook_url or "").strip() or None,
            feishu_webhooks=body.feishu_webhooks,
            feishu_user_ids=body.feishu_user_ids,
            notification_channels=body.notification_channels,
            channel_config=body.channel_config,
            feishu_push=bool(body.feishu_push),
            enabled=bool(body.enabled),
        )
    elif task_type == TASK_TYPE_BIM_DAILY_ANALYSIS:
        result = svc.create_bim_analysis_task(
            cron=cron_expr,
            name=(body.name or "").strip() or "商机每日综合分析",
            top_n=max(1, min(int(body.top_n or 10), 30)),
            feishu_webhook_url=(body.feishu_webhook_url or "").strip() or None,
            feishu_webhooks=body.feishu_webhooks,
            feishu_user_ids=body.feishu_user_ids,
            enabled=bool(body.enabled),
        )
    elif task_type in (TASK_TYPE_BIM_DAILY_BRIEF, TASK_TYPE_BIM_WEEKLY_BRIEF):
        result = svc.create_brief_task(
            task_type=task_type,
            cron=cron_expr,
            name=(body.name or "").strip(),
            top_n=max(1, min(int(body.top_n or 10), 30)),
            days=max(1, min(int(body.days or (30 if task_type == TASK_TYPE_BIM_WEEKLY_BRIEF else 1)), 31)),
            feishu_webhook_url=(body.feishu_webhook_url or "").strip() or None,
            feishu_webhooks=body.feishu_webhooks,
            feishu_user_ids=body.feishu_user_ids,
            feishu_push=bool(body.feishu_push),
            enabled=bool(body.enabled),
        )
    elif task_type == TASK_TYPE_ENGINEERING_LLM_REPORT:
        if not body.feishu_user_ids:
            raise HTTPException(status_code=400, detail="engineering_llm_report 需要 feishu_user_ids")
        result = svc.create_engineering_llm_report_task(
            cron=cron_expr,
            name=(body.name or "").strip() or "工程大模型应用报告",
            feishu_user_ids=body.feishu_user_ids,
            feishu_webhooks=body.feishu_webhooks,
            days=max(1, min(int(body.days or 1), 31)),
            top_n=max(1, min(int(body.top_n or 10), 30)),
            feishu_push=bool(body.feishu_push),
            enabled=bool(body.enabled),
        )
    else:
        raise HTTPException(status_code=400, detail=f"不支持的 task_type: {task_type}")

    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "创建失败")
    return result


@app.post("/api/chat/scheduled-tasks/bim-analysis")
async def api_create_bim_analysis_schedule(body: BimAnalysisScheduleRequest):
    from src.web.chat_schedule_service import get_chat_schedule_service

    cron_expr = _resolve_cron_from_request(
        body.cron,
        body.cron_expression,
        body.cron_preset,
        body.cron_custom,
    )

    result = get_chat_schedule_service().create_bim_analysis_task(
        cron=cron_expr,
        name=(body.name or "").strip() or "商机每日综合分析",
        top_n=max(1, min(int(body.top_n or 10), 30)),
        feishu_webhook_url=(body.feishu_webhook_url or "").strip() or None,
        enabled=bool(body.enabled),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "创建失败")
    return result


@app.get("/api/chat/scheduled-tasks")
async def api_list_scheduled_tasks(enabled_only: bool = False):
    from src.web.chat_schedule_service import get_chat_schedule_service

    return get_chat_schedule_service().list_tasks(enabled_only=enabled_only)


@app.get("/api/chat/scheduled-tasks/{task_id}")
async def api_get_scheduled_task(task_id: str):
    from src.web.chat_schedule_service import get_chat_schedule_service

    result = get_chat_schedule_service().get_task(task_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error") or "任务不存在")
    return result


@app.patch("/api/chat/scheduled-tasks/{task_id}")
async def api_patch_scheduled_task(task_id: str, body: ScheduledTaskPatchRequest):
    from src.core.chat_scheduled_tasks import (
        TASK_TYPE_ENGINEERING_LLM_REPORT,
        get_chat_scheduled_task,
        is_hermes_task_type,
        parse_feishu_user_ids,
    )
    from src.web.chat_schedule_service import get_chat_schedule_service

    existing = get_chat_scheduled_task(task_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="至少提供一个更新字段")

    cron_preset = fields.pop("cron_preset", None)
    cron_custom = fields.pop("cron_custom", None)
    cron_expression = fields.pop("cron_expression", None)
    if "cron" in fields or cron_expression is not None or cron_preset is not None:
        fields["cron"] = _resolve_cron_from_request(
            fields.pop("cron", None),
            cron_expression,
            cron_preset,
            cron_custom,
        )

    effective_type = (fields.get("task_type") or existing.get("task_type") or "hermes_summary").strip()
    if is_hermes_task_type(effective_type):
        prompt = (
            fields.get("hermes_prompt")
            or fields.get("prompt")
            or existing.get("hermes_prompt")
            or existing.get("prompt")
            or ""
        ).strip()
        if "hermes_prompt" in fields or "prompt" in fields:
            if not prompt:
                raise HTTPException(status_code=400, detail="Hermes 任务需要填写提示词")
            fields["hermes_prompt"] = prompt
            fields.pop("prompt", None)
    elif effective_type == TASK_TYPE_ENGINEERING_LLM_REPORT:
        if "feishu_user_ids" in fields:
            user_ids = parse_feishu_user_ids(fields["feishu_user_ids"])
            if not user_ids:
                raise HTTPException(status_code=400, detail="请填写飞书 user_id 列表")
            fields["feishu_user_ids"] = user_ids

    result = get_chat_schedule_service().update_task(task_id, **fields)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "更新失败")
    return result


@app.post("/api/chat/scheduled-tasks/{task_id}/toggle")
async def api_toggle_scheduled_task(task_id: str, body: Optional[ScheduledTaskToggleRequest] = None):
    from src.web.chat_schedule_service import get_chat_schedule_service

    enabled = body.enabled if body is not None else None
    result = get_chat_schedule_service().toggle_task(task_id, enabled=enabled)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "切换失败")
    return result


@app.delete("/api/chat/scheduled-tasks/{task_id}")
async def api_delete_scheduled_task(task_id: str):
    from src.web.chat_schedule_service import get_chat_schedule_service

    result = get_chat_schedule_service().delete_task(task_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "删除失败")
    return result


@app.post("/api/chat/scheduled-tasks/{task_id}/run")
async def api_run_scheduled_task(
    task_id: str,
    body: Optional[ScheduledTaskRunRequest] = None,
    dry_run: bool = Query(False, description="兼容 query 参数 dry_run"),
):
    from src.web.chat_schedule_service import get_chat_schedule_service

    effective_dry_run = body.dry_run if body is not None else dry_run
    result = await get_chat_schedule_service().run_now(task_id, dry_run=effective_dry_run, force=True)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error") or result.get("message") or "执行失败")
    return result


@app.get("/api/sites/onboarding/{site_id}/status")
async def api_sites_onboarding_status(site_id: str):
    from src.core.site_onboarding import ONBOARDING_STAGES, STAGE_LABELS, get_onboarding_status

    status = get_onboarding_status(site_id.strip())
    if status is None:
        raise HTTPException(status_code=404, detail=f"站点不存在: {site_id}")
    payload = status.model_dump(mode="json")
    payload["stage_labels"] = STAGE_LABELS
    payload["stage_order"] = ONBOARDING_STAGES
    return {"ok": True, **payload}


@app.get("/api/sites/resolve")
async def api_sites_resolve(query: str = Query(..., min_length=1)):
    from src.web.browser_crawl import infer_site_id_from_url
    from src.web.crawl_agent_tools import resolve_site_id

    q = query.strip()
    site_id = resolve_site_id(q) or infer_site_id_from_url(q)
    if not site_id:
        return {"ok": False, "query": q, "error": f"无法从「{q}」解析 site_id"}
    site = get_site_by_id(site_id)
    return {
        "ok": True,
        "query": q,
        "site_id": site_id,
        "site_name": (site or {}).get("name"),
        "url": (site or {}).get("url"),
        "registered": site is not None,
    }


@app.get("/onboard")
@app.get("/onboard/{site_id}")
async def onboard_redirect(site_id: Optional[str] = None):
    from fastapi.responses import RedirectResponse

    target = "/hermes?intent=onboard"
    if site_id:
        target += f"&site_id={site_id.strip()}"
    return RedirectResponse(url=target, status_code=302)


@app.post("/api/browser/crawl")
async def api_browser_crawl(body: BrowserCrawlRequest):
    from src.web.browser_crawl import browser_auto_crawl, infer_site_id_from_url

    session_id = (body.session_id or "").strip() or "browser-ui"
    site_id = (body.site_id or "").strip()
    if not site_id:
        from src.web.webbridge_skills import webbridge_snapshot

        snap = webbridge_snapshot(session_id=session_id)
        if not snap.get("ok"):
            raise HTTPException(
                status_code=400,
                detail=snap.get("error") or "无法获取当前页面，请先打开列表页",
            )
        site_id = infer_site_id_from_url(snap.get("url") or "") or ""
    if not site_id:
        raise HTTPException(
            status_code=400,
            detail="无法从 URL 推断 site_id，请手动选择站点",
        )
    if body.max_pages is not None and body.max_pages < 1:
        raise HTTPException(status_code=400, detail="max_pages 须 ≥ 1")

    result = browser_auto_crawl(
        session_id=session_id,
        site_id=site_id,
        max_pages=body.max_pages,
        save=body.save,
        confirm_generic=body.confirm_generic,
    )
    ok = result.get("ok") is not False
    if not ok and "未注册" in (result.get("error") or ""):
        raise HTTPException(status_code=404, detail=result.get("error"))
    if not ok and "频繁" in (result.get("error") or ""):
        raise HTTPException(status_code=429, detail=result.get("error"))
    if not ok:
        raise HTTPException(status_code=400, detail=result.get("error") or "爬取失败")
    return {"ok": ok, "result": result, "error": result.get("error")}



@app.get("/api/agent/logs")
async def api_agent_logs(
    tail: int = Query(500, ge=1, le=5000),
    format: str = Query("json", pattern="^(json|text)$"),
):
    """读取 agent-crawl.log 尾部 N 行。"""
    crawl_logger = get_agent_crawl_logger()
    lines = crawl_logger.tail_lines(tail)
    if format == "text":
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(
            "\n".join(lines) if lines else "",
            media_type="text/plain; charset=utf-8",
        )
    return {
        "path": str(crawl_logger.main_log_path),
        "tail": tail,
        "lines": lines,
        "count": len(lines),
    }
