"""股票领域分析 — 独立 FastAPI 应用（默认端口 8092）。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.concurrency import run_in_threadpool
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field, model_validator

from src.core.policy_news_fetcher import DISCLAIMER as POLICY_DISCLAIMER, get_policy_news_fetcher
from src.core.stock_news_fetcher import DISCLAIMER, get_stock_news_fetcher

from src.web.crawl_agent_routes import register_crawl_agent_routes
from src.web.policy_insights_service import get_policy_insights_service
from src.web.data_service import get_data_service
from src.web.stock_insights_service import (
    get_agri_ui_port,
    get_main_ui_port,
    get_stock_insights_service,
    get_stock_ui_port,
)

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="WebScraperAgent",
    description="WebScraperAgent — 股票领域分析专题",
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
_news_sync_running = False
_last_news_sync_summary: dict[str, Any] | None = None
_policy_sync_running = False
_last_policy_sync_summary: dict[str, Any] | None = None


class InvestmentPlanRequest(BaseModel):
    industries: list[str] = Field(default_factory=list, description="关注产业/板块")
    sector: str | None = Field(default=None, description="单个产业别名（与 industries 二选一）")
    risk_profile: str = Field(default="balanced", description="conservative|balanced|aggressive")
    horizon_months: int = Field(default=12, ge=6, le=24)
    days: int = Field(default=7, ge=1, le=90)
    use_llm: bool = True


def _stock_page_context(**extra: Any) -> dict[str, Any]:
    return {
        "main_ui_port": get_main_ui_port(),
        "agri_ui_port": get_agri_ui_port(),
        "stock_ui_port": get_stock_ui_port(),
        **extra,
    }


def _render(name: str, context: dict[str, Any], *, status_code: int = 200) -> HTMLResponse:
    template = _jinja_env.get_template(name)
    return HTMLResponse(template.render(**context), status_code=status_code)


register_crawl_agent_routes(
    app,
    render=_render,
    default_agent_profile="stock",
    chat_template="stock_hermes.html",
    include_chat_alias=False,
    include_pending_page=False,
)


@app.on_event("startup")
async def _warm_overseas_search_on_startup() -> None:
    from src.core.stock_search_service import warm_overseas_search_cache

    asyncio.get_event_loop().run_in_executor(None, warm_overseas_search_cache)


async def _run_policy_sync_job() -> None:
    global _policy_sync_running, _last_policy_sync_summary
    try:
        result = get_policy_news_fetcher().fetch_all(days=30)
        get_policy_insights_service().refresh_cache()
        _last_policy_sync_summary = {"ok": True, **result}
    except Exception as exc:
        logger.exception("政策同步失败: %s", exc)
        _last_policy_sync_summary = {"ok": False, "error": str(exc)}
    finally:
        _policy_sync_running = False


async def _run_stock_sync_job() -> None:
    global _sync_running, _last_sync_summary, _news_sync_running, _last_news_sync_summary
    global _policy_sync_running, _last_policy_sync_summary
    try:
        news_result = get_stock_news_fetcher().fetch_all(days=7)
        policy_result = get_policy_news_fetcher().fetch_all(days=30)
        get_policy_insights_service().refresh_cache()
        summary = get_stock_insights_service().refresh_cache()
        _last_sync_summary = {**summary, "news": news_result, "policy": policy_result}
        _last_news_sync_summary = {"ok": True, **news_result}
        _last_policy_sync_summary = {"ok": True, **policy_result}
    except Exception as exc:
        logger.exception("股票同步失败: %s", exc)
        _last_sync_summary = {"success": False, "error": str(exc)}
        _last_news_sync_summary = {"ok": False, "error": str(exc)}
    finally:
        _sync_running = False
        _news_sync_running = False


async def _run_stock_news_sync_job() -> None:
    global _news_sync_running, _last_news_sync_summary
    try:
        result = get_stock_news_fetcher().fetch_all(days=7)
        get_stock_insights_service().refresh_cache()
        _last_news_sync_summary = {"ok": True, **result}
    except Exception as exc:
        logger.exception("股票新闻同步失败: %s", exc)
        _last_news_sync_summary = {"ok": False, "error": str(exc)}
    finally:
        _news_sync_running = False


@app.get("/", response_class=HTMLResponse)
async def stock_page(
    request: Request,
    days: int = Query(7, ge=1, le=90),
    use_llm: bool = Query(False, description="是否请求 LLM 分析（默认仅统计；点「LLM 重新分析」或传 true 启用）"),
    refresh_llm: bool = Query(False, description="忽略缓存重新生成 LLM 分析"),
    industry: str | None = Query(None, description="产业发展分析目标产业"),
):
    svc = get_data_service()
    ctx = await run_in_threadpool(
        get_stock_insights_service().get_page_context,
        days=days,
        use_llm=use_llm,
        refresh_llm=refresh_llm,
        industry=industry,
    )
    for _dup in ("days", "main_ui_port", "agri_ui_port", "stock_ui_port"):
        ctx.pop(_dup, None)
    page_ctx = _stock_page_context(
        request=request,
        data_source=svc.data_source,
        sync_running=_sync_running,
        days=days,
        active_nav="overview",
        **ctx,
    )
    return _render("stock.html", page_ctx)


@app.get("/insights", response_class=HTMLResponse)
async def stock_insights_page(request: Request):
    svc = get_data_service()
    page_ctx = _stock_page_context(
        request=request,
        data_source=svc.data_source,
        active_nav="insights",
        indices={},
        disclaimer=DISCLAIMER,
    )
    return _render("stock_insights.html", page_ctx)


@app.get("/api/stock/latest")
async def api_stock_latest(
    limit: int = Query(20, ge=1, le=100),
    days: int = Query(7, ge=1, le=90),
):
    svc = get_data_service()
    result = get_stock_insights_service().get_latest(limit=limit, days=days)
    return {"data_source": svc.data_source, **result}


@app.get("/api/stock/insights")
async def api_stock_insights(
    days: int = Query(7, ge=1, le=90),
    use_llm: bool = Query(False),
    refresh_llm: bool = Query(False),
):
    svc = get_data_service()
    result = get_stock_insights_service().get_insights(
        days=days,
        use_llm=use_llm,
        refresh_llm=refresh_llm,
    )
    return {"data_source": svc.data_source, **result}


@app.post("/api/stock/sync")
async def api_stock_sync(background_tasks: BackgroundTasks):
    global _sync_running, _news_sync_running
    if _sync_running or _news_sync_running:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": "股票同步任务已在运行",
                "running": True,
                "last_summary": _last_sync_summary,
                "last_news_summary": _last_news_sync_summary,
            },
        )
    _sync_running = True
    _news_sync_running = True
    background_tasks.add_task(_run_stock_sync_job)
    return {
        "ok": True,
        "message": "股票同步已启动（新闻拉取 + 洞察缓存刷新）",
        "running": True,
        "disclaimer": DISCLAIMER,
    }


@app.get("/api/stock/sync/status")
async def api_stock_sync_status():
    return {
        "ok": True,
        "running": _sync_running,
        "last_summary": _last_sync_summary,
    }


@app.get("/api/stock/health")
async def api_stock_health():
    return {
        "ok": True,
        "service": "stock_ui",
        "main_ui_port": get_main_ui_port(),
        "agri_ui_port": get_agri_ui_port(),
        "sync_running": _sync_running,
        "news_sync_running": _news_sync_running,
        "policy_sync_running": _policy_sync_running,
        "disclaimer": DISCLAIMER,
    }


@app.get("/api/stock/news")
async def api_stock_news(
    limit: int = Query(30, ge=1, le=100),
    days: int = Query(7, ge=1, le=90),
    industry: str | None = Query(None),
):
    result = get_stock_news_fetcher().list_news(limit=limit, days=days, industry=industry)
    return result


@app.post("/api/stock/news/sync")
async def api_stock_news_sync(background_tasks: BackgroundTasks):
    global _news_sync_running
    if _news_sync_running:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": "股票新闻同步任务已在运行",
                "running": True,
                "last_summary": _last_news_sync_summary,
            },
        )
    _news_sync_running = True
    background_tasks.add_task(_run_stock_news_sync_job)
    return {
        "ok": True,
        "message": "股票新闻同步已启动",
        "running": True,
        "disclaimer": DISCLAIMER,
    }


@app.get("/api/stock/news/sync/status")
async def api_stock_news_sync_status():
    return {
        "ok": True,
        "running": _news_sync_running,
        "last_summary": _last_news_sync_summary,
    }


@app.get("/api/stock/analysis")
async def api_stock_analysis(
    days: int = Query(7, ge=1, le=90),
    industry: str | None = Query(None),
    sector: str | None = Query(None, description="产业别名，同 industry"),
    use_llm: bool = Query(True),
    refresh_llm: bool = Query(False),
):
    svc = get_data_service()
    target = (sector or industry or "").strip() or None
    result = get_stock_insights_service().get_analysis(
        industry=target,
        days=days,
        use_llm=use_llm,
        refresh_llm=refresh_llm,
    )
    return {"data_source": svc.data_source, **result}


@app.get("/api/stock/market")
async def api_stock_market():
    from src.core.stock_market_data import get_stock_market_data

    snapshot = await run_in_threadpool(get_stock_market_data().get_market_snapshot)
    return {"ok": True, **snapshot}


@app.get("/api/stock/sectors/classifications")
async def api_stock_sector_classifications():
    from src.core.stock_market_data import get_stock_market_data

    return await run_in_threadpool(get_stock_market_data().list_classifications)


@app.get("/api/stock/sectors")
async def api_stock_sectors(
    classify: str = Query("industry", description="industry|concept|sw_l1|sw_l2|sw_l3"),
    limit: int = Query(100, ge=1, le=200),
    sort_by: str = Query(
        "change_pct",
        description="change_pct|amount|market_cap|turnover_rate|rank|name|constituent_count|pe_ttm|pb",
    ),
    order: str = Query("desc", description="asc|desc"),
    enrich_amount: bool = Query(False, description="是否拉取各板块实时成交额（较慢）"),
):
    from src.core.stock_market_data import get_stock_market_data

    result = await run_in_threadpool(
        get_stock_market_data().get_sectors_trading,
        classify=classify,
        limit=limit,
        sort_by=sort_by,
        order=order,
        enrich_amount=enrich_amount or sort_by == "amount",
    )
    return {"ok": result.get("ok", False), **result}


@app.get("/api/stock/sectors/detail")
async def api_stock_sector_detail(
    symbol: str = Query("", description="板块/行业名称"),
    code: str | None = Query(None, description="申万等行业代码（如 801010）"),
    classify: str = Query("industry", description="industry|concept|sw_l1|sw_l2|sw_l3"),
    cons_limit: int = Query(30, ge=1, le=100),
):
    from src.core.stock_market_data import get_stock_market_data

    result = await run_in_threadpool(
        get_stock_market_data().get_sector_detail,
        symbol,
        classify=classify,
        code=code,
        cons_limit=cons_limit,
    )
    return result


@app.get("/api/stock/sectors/industry-chain")
async def api_stock_sector_industry_chain(
    symbol: str = Query("", description="板块/行业名称"),
    code: str | None = Query(None, description="申万等行业代码"),
    classify: str = Query("industry", description="industry|concept|sw_l1|sw_l2|sw_l3"),
    cons_limit: int = Query(5, ge=1, le=20),
):
    from src.core.stock_industry_chain_service import get_stock_industry_chain_service

    result = await run_in_threadpool(
        get_stock_industry_chain_service().get_sector_industry_chain,
        classify,
        symbol,
        code,
        cons_limit=cons_limit,
    )
    return result


@app.get("/api/stock/search")
async def api_stock_search(
    q: str = Query("", description="代码/名称/拼音首字母"),
    limit: int = Query(20, ge=1, le=100),
    refresh: bool = Query(False, description="强制刷新检索索引"),
):
    from src.core.stock_search_service import get_stock_search_service

    result = await run_in_threadpool(
        get_stock_search_service().search,
        q,
        limit=limit,
        refresh=refresh,
    )
    return result


@app.get("/search", response_class=HTMLResponse)
async def stock_search_page(
    request: Request,
    q: str = Query("", description="检索关键词"),
):
    svc = get_data_service()
    page_ctx = _stock_page_context(
        request=request,
        data_source=svc.data_source,
        active_nav="search",
        query=(q or "").strip(),
        disclaimer=DISCLAIMER,
    )
    return _render("stock_search.html", page_ctx)


@app.get("/stock/kline/{code}", response_class=HTMLResponse)
async def stock_kline_page(
    request: Request,
    code: str,
    name: str | None = Query(None, description="股票名称（可选）"),
    market: str | None = Query(None, description="SH|SZ|BJ|HK|US"),
    period: str = Query("daily", description="daily|weekly|monthly"),
):
    svc = get_data_service()
    page_ctx = _stock_page_context(
        request=request,
        data_source=svc.data_source,
        active_nav="insights",
        code=code.strip(),
        stock_name=(name or "").strip(),
        market=(market or "").strip().upper(),
        period=period,
        disclaimer=DISCLAIMER,
    )
    return _render("stock_kline.html", page_ctx)


@app.get("/stock/hk/{code}", response_class=HTMLResponse)
async def stock_hk_detail_page(
    request: Request,
    code: str,
    name: str | None = Query(None, description="股票名称（可选）"),
):
    return await stock_detail_page(request, code, name=name, market="HK")


@app.get("/stock/hk/{code}/kline", response_class=HTMLResponse)
async def stock_hk_kline_page(
    request: Request,
    code: str,
    name: str | None = Query(None, description="股票名称（可选）"),
    period: str = Query("daily", description="daily|weekly|monthly"),
):
    return await stock_kline_page(request, code, name=name, market="HK", period=period)


@app.get("/stock/us/{code}", response_class=HTMLResponse)
async def stock_us_detail_page(
    request: Request,
    code: str,
    name: str | None = Query(None, description="股票名称（可选）"),
):
    return await stock_detail_page(request, code, name=name, market="US")


@app.get("/stock/us/{code}/kline", response_class=HTMLResponse)
async def stock_us_kline_page(
    request: Request,
    code: str,
    name: str | None = Query(None, description="股票名称（可选）"),
    period: str = Query("daily", description="daily|weekly|monthly"),
):
    return await stock_kline_page(request, code, name=name, market="US", period=period)


@app.get("/stock/{code}", response_class=HTMLResponse)
async def stock_detail_page(
    request: Request,
    code: str,
    name: str | None = Query(None, description="股票名称（可选）"),
    market: str | None = Query(None, description="SH|SZ|BJ|HK|US"),
):
    svc = get_data_service()
    page_ctx = _stock_page_context(
        request=request,
        data_source=svc.data_source,
        active_nav="insights",
        code=code.strip(),
        stock_name=(name or "").strip(),
        market=(market or "").strip().upper(),
        disclaimer=DISCLAIMER,
    )
    return _render("stock_detail.html", page_ctx)


@app.get("/api/stock/company/{code}")
async def api_stock_company(
    code: str,
    market: str | None = Query(None, description="SH|SZ|BJ|HK|US"),
    periods: int = Query(4, ge=1, le=8, description="资产负债表期数"),
    refresh: bool = Query(False, description="忽略缓存重新拉取"),
):
    from src.core.stock_company_service import get_stock_company_service

    result = await run_in_threadpool(
        get_stock_company_service().get_stock_detail,
        code,
        market=market,
        balance_periods=periods,
        refresh=refresh,
    )
    return result


@app.get("/api/stock/company/{code}/balance-sheet")
async def api_stock_company_balance_sheet(
    code: str,
    market: str | None = Query(None, description="SH|SZ|BJ|HK|US"),
    periods: int = Query(4, ge=1, le=8),
    refresh: bool = Query(False),
):
    from src.core.stock_company_service import get_stock_company_service

    result = await run_in_threadpool(
        get_stock_company_service().get_balance_sheet,
        code,
        market=market,
        periods=periods,
        refresh=refresh,
    )
    return result


class CompanySyncRequest(BaseModel):
    code: str | None = Field(default=None, description="单只股票代码")
    codes: list[str] | None = Field(default=None, description="批量股票代码")
    market: str | None = Field(default=None, description="SH|SZ|BJ|HK|US")
    periods: int = Field(default=4, ge=1, le=8, description="资产负债表期数")


@app.post("/api/stock/company/sync")
async def api_stock_company_sync(body: CompanySyncRequest):
    from src.core.stock_company_service import get_stock_company_service

    svc = get_stock_company_service()
    if body.codes:
        result = await run_in_threadpool(
            svc.sync_company_batch,
            body.codes,
            market=body.market,
            balance_periods=body.periods,
        )
        return result
    if body.code:
        result = await run_in_threadpool(
            svc.sync_company_detail,
            body.code,
            market=body.market,
            balance_periods=body.periods,
        )
        return result
    return JSONResponse(status_code=400, content={"ok": False, "error": "code 或 codes 必填"})


@app.get("/api/stock/company/{code}/meta")
async def api_stock_company_meta(code: str, market: str | None = Query(None)):
    from src.core.stock_company_store import get_stock_company_store

    result = await run_in_threadpool(
        lambda c=code, m=market: get_stock_company_store().get_meta(c, market=m)
    )
    return result


@app.get("/api/stock/kline")
async def api_stock_kline(
    code: str = Query(..., description="股票代码，如 000001 或 00700"),
    market: str | None = Query(None, description="SH|SZ|BJ|HK|US"),
    period: str = Query("daily", description="daily|weekly|monthly"),
    limit: int = Query(120, ge=1, le=500),
    auto_fetch: bool = Query(True, description="本地无数据时自动拉取 AKShare"),
):
    from src.core.stock_kline_service import get_stock_kline_service

    result = await run_in_threadpool(
        get_stock_kline_service().get_kline,
        code,
        market=market,
        period=period,
        limit=limit,
        auto_fetch=auto_fetch,
    )
    return result


@app.get("/api/stock/kline/meta")
async def api_stock_kline_meta(
    code: str = Query(..., description="股票代码"),
    market: str | None = Query(None, description="SH|SZ|BJ|HK|US"),
    period: str = Query("daily", description="daily|weekly|monthly"),
):
    from src.core.stock_kline_service import get_stock_kline_service

    result = await run_in_threadpool(
        get_stock_kline_service().get_kline_meta,
        code,
        market=market,
        period=period,
    )
    return result


class KlineSyncRequest(BaseModel):
    code: str = Field(..., description="股票代码")
    market: str | None = Field(default=None, description="SH|SZ|BJ|HK|US")
    period: str = Field(default="daily", description="daily|weekly|monthly")
    start_date: str | None = Field(default=None, description="YYYYMMDD 或 YYYY-MM-DD")
    end_date: str | None = Field(default=None, description="YYYYMMDD 或 YYYY-MM-DD")
    name: str | None = Field(default=None, description="股票名称（可选）")


@app.post("/api/stock/kline/sync")
async def api_stock_kline_sync(body: KlineSyncRequest):
    from src.core.stock_kline_service import get_stock_kline_service

    result = await run_in_threadpool(
        get_stock_kline_service().fetch_and_save_kline,
        body.code,
        market=body.market,
        period=body.period,
        start_date=body.start_date,
        end_date=body.end_date,
        name=body.name,
    )
    return result


@app.get("/api/stock/hk/overview")
async def api_stock_hk_overview():
    from src.core.stock_market_data import get_stock_market_data

    result = await run_in_threadpool(get_stock_market_data().get_hk_index_overview)
    return {"ok": True, **result}


@app.get("/api/stock/us/overview")
async def api_stock_us_overview():
    from src.core.stock_market_data import get_stock_market_data

    result = await run_in_threadpool(get_stock_market_data().get_us_index_overview)
    return {"ok": True, **result}


@app.post("/api/stock/investment-plan")
async def api_stock_investment_plan(body: InvestmentPlanRequest):
    industries = body.industries or []
    if body.sector and body.sector.strip():
        industries = [body.sector.strip(), *industries]
    result = get_stock_insights_service().generate_investment_plan(
        industries=industries or None,
        risk_profile=body.risk_profile,
        horizon_months=body.horizon_months,
        days=body.days,
        use_llm=body.use_llm,
    )
    return {"ok": True, **result}


@app.get("/api/stock/policy/latest")
@app.get("/api/policy/insights")
async def api_policy_insights(
    days: int = Query(30, ge=1, le=90),
    limit: int = Query(30, ge=1, le=100),
    use_llm: bool = Query(True),
    refresh_llm: bool = Query(False),
    theme: str | None = Query(None),
):
    if theme:
        result = get_policy_insights_service().get_latest(limit=limit, days=days, theme=theme)
    else:
        result = get_policy_insights_service().get_insights(
            days=days,
            use_llm=use_llm,
            refresh_llm=refresh_llm,
        )
    return {"ok": True, **result}


@app.get("/api/stock/investment-direction")
async def api_investment_direction(
    days: int = Query(30, ge=1, le=90),
    use_llm: bool = Query(True),
    refresh_llm: bool = Query(False),
):
    result = get_policy_insights_service().generate_investment_direction(
        days=days,
        use_llm=use_llm,
        refresh_llm=refresh_llm,
    )
    return {"ok": True, **result}


@app.get("/api/stock/policy/news")
async def api_policy_news(
    limit: int = Query(30, ge=1, le=100),
    days: int = Query(30, ge=1, le=90),
    theme: str | None = Query(None),
    source_id: str | None = Query(None),
):
    result = get_policy_news_fetcher().list_news(
        limit=limit,
        days=days,
        theme=theme,
        source_id=source_id,
    )
    return result


@app.post("/api/stock/policy/sync")
async def api_policy_sync(background_tasks: BackgroundTasks):
    global _policy_sync_running
    if _policy_sync_running:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": "政策同步任务已在运行",
                "running": True,
                "last_summary": _last_policy_sync_summary,
            },
        )
    _policy_sync_running = True
    background_tasks.add_task(_run_policy_sync_job)
    return {
        "ok": True,
        "message": "政府政策同步已启动",
        "running": True,
        "disclaimer": POLICY_DISCLAIMER,
    }


@app.get("/api/stock/policy/sync/status")
async def api_policy_sync_status():
    return {
        "ok": True,
        "running": _policy_sync_running,
        "last_summary": _last_policy_sync_summary,
    }


# ── 模拟交易 ──────────────────────────────────────────────────


class SimulatorInitRequest(BaseModel):
    account_id: str = Field(default="default", description="账户 ID")
    initial_cash: float = Field(default=1_000_000.0, ge=1000, description="初始资金")
    reset: bool = Field(default=False, description="重置账户与流水")


class SimulatorBuyRequest(BaseModel):
    account_id: str = Field(default="default")
    code: str = Field(..., description="股票代码")
    market: str | None = Field(default=None, description="SH|SZ|BJ|HK|US")
    name: str | None = Field(default=None, description="股票名称")
    quantity: int | None = Field(default=None, ge=1, description="买入股数")
    amount: float | None = Field(default=None, ge=0, description="买入金额（与 quantity 二选一）")
    price: float | None = Field(default=None, ge=0, description="指定成交价，默认现价/K线收盘价")


class SimulatorSellRequest(BaseModel):
    account_id: str = Field(default="default")
    code: str = Field(..., description="股票代码")
    market: str | None = Field(default=None)
    quantity: int = Field(..., ge=1, description="卖出股数")
    price: float | None = Field(default=None, ge=0, description="指定成交价")


class SimulatorRulesRequest(BaseModel):
    account_id: str = Field(default="default")
    stop_loss_pct: float | None = Field(default=None, ge=0, le=100)
    take_profit_pct: float | None = Field(default=None, ge=0, le=500)
    max_position_pct: float | None = Field(default=None, ge=1, le=100)
    ma_cross_enabled: bool | None = None
    change_pct_enabled: bool | None = None
    change_pct_trigger: float | None = Field(default=None, ge=0, le=50)


class SimulatorUniverseConfig(BaseModel):
    classify: str = Field(default="concept", description="industry|concept|sw_l1|sw_l2|sw_l3")
    symbol: str = Field(default="", description="板块/概念名称")
    code: str | None = Field(default=None, description="申万等行业代码")
    limit: int = Field(default=5, ge=1, le=20, description="选股数量")
    universe: str = Field(default="sector", description="all|sector|concept|sw_l1|watchlist")


class SimulatorPickStocksRequest(BaseModel):
    """自动选股请求体（POST /api/stock/simulator/pick-stocks）。

    pick_strategy 可选：agent | rules | sector_momentum | value | growth | garp |
    momentum | dual_ma | quality | sector_rotation | low_volatility
    """

    pick_strategy: str = Field(
        default="rules",
        description=(
            "选股策略：agent|rules|sector_momentum|value|growth|garp|"
            "momentum|dual_ma|quality|sector_rotation|low_volatility"
        ),
    )
    universe: SimulatorUniverseConfig | None = Field(default=None, description="选股范围")
    start_date: str | None = Field(default=None, description="回测起始日（历史模式参考）")
    end_date: str | None = Field(default=None, description="回测结束日（仅文档，不参与 PIT 决策）")
    as_of_date: str | None = Field(default=None, description="决策时点 YYYY-MM-DD，仅使用 date<=as_of_date 的数据")
    momentum_lookback: int = Field(default=5, ge=5, le=60, description="动量窗口（交易日近似）")
    constraints: dict[str, Any] | None = Field(default=None, description="策略参数：pe_max/pb_max/roe_min/peg_max 等")


def _simulator_universe_config(cfg: SimulatorUniverseConfig | None) -> SimulatorUniverseConfig:
    return cfg or SimulatorUniverseConfig()


def _simulator_pick_kwargs(
    *,
    pick_strategy: str,
    universe: SimulatorUniverseConfig | None,
    start_date: str | None = None,
    end_date: str | None = None,
    constraints: dict[str, Any] | None = None,
    momentum_lookback: int = 5,
) -> dict[str, Any]:
    u = _simulator_universe_config(universe)
    return {
        "universe": u.universe,
        "classify": u.classify,
        "symbol": u.symbol,
        "code": u.code,
        "limit": u.limit,
        "strategy": pick_strategy,
        "start_date": start_date,
        "end_date": end_date,
        "constraints": constraints,
        "momentum_lookback": momentum_lookback,
    }


class SimulatorBacktestRequest(BaseModel):
    codes: list[str] = Field(default_factory=list, description="股票代码列表")
    auto_pick: bool = Field(default=False, description="是否自动选股后再回测")
    pick_strategy: str = Field(
        default="rules",
        description=(
            "自动选股策略：agent|rules|sector_momentum|value|growth|garp|"
            "momentum|dual_ma|quality|sector_rotation|low_volatility"
        ),
    )
    universe: SimulatorUniverseConfig | None = Field(default=None, description="自动选股范围")
    rebalance_interval: int = Field(default=20, ge=1, le=120, description="自动选股再平衡间隔（交易日）")
    start_date: str = Field(..., description="回测起始日 YYYY-MM-DD")
    end_date: str = Field(..., description="回测结束日 YYYY-MM-DD")
    initial_cash: float = Field(default=1_000_000.0, ge=1000, description="初始资金")
    strategy: str = Field(default="rules", description="rules | agent")
    rules: dict[str, Any] | None = Field(default=None, description="止损止盈/MA 等规则")
    momentum_lookback: int = Field(default=5, ge=5, le=60, description="选股动量窗口")
    pick_constraints: dict[str, Any] | None = Field(default=None, description="选股策略参数 pe_max 等")
    async_run: bool = Field(default=False, alias="async", description="是否异步执行")

    @model_validator(mode="after")
    def _codes_or_auto_pick(self) -> "SimulatorBacktestRequest":
        if not self.auto_pick and not self.codes:
            raise ValueError("codes 不能为空，或开启 auto_pick")
        return self


class SimulatorBacktestAgentRequest(SimulatorBacktestRequest):
    agent_interval: int = Field(default=5, ge=1, le=30, description="Agent 决策间隔（交易日）")
    lookback_days: int = Field(default=30, ge=5, le=120, description="Agent 历史窗口天数")
    timeout: int = Field(default=30, ge=5, le=120, description="单次 LLM 超时秒数")


@app.get("/simulator", response_class=HTMLResponse)
async def stock_simulator_page(request: Request):
    from src.core.stock_simulator_service import DISCLAIMER as SIM_DISCLAIMER

    svc = get_data_service()
    page_ctx = _stock_page_context(
        request=request,
        data_source=svc.data_source,
        active_nav="simulator",
        disclaimer=SIM_DISCLAIMER,
    )
    return _render("stock_simulator.html", page_ctx)


@app.get("/api/stock/simulator/account")
async def api_simulator_get_account(
    account_id: str = Query("default"),
    initial_cash: float | None = Query(None, ge=1000),
):
    from src.core.stock_simulator_service import get_stock_simulator_service

    result = await run_in_threadpool(
        get_stock_simulator_service().get_account,
        account_id,
        initial_cash=initial_cash,
    )
    return result


@app.post("/api/stock/simulator/account")
async def api_simulator_init_account(body: SimulatorInitRequest):
    from src.core.stock_simulator_service import get_stock_simulator_service

    result = await run_in_threadpool(
        get_stock_simulator_service().init_account,
        body.account_id,
        initial_cash=body.initial_cash,
        reset=body.reset,
    )
    return result


@app.post("/api/stock/simulator/buy")
async def api_simulator_buy(body: SimulatorBuyRequest):
    from src.core.stock_simulator_service import get_stock_simulator_service

    result = await run_in_threadpool(
        get_stock_simulator_service().buy,
        account_id=body.account_id,
        code=body.code,
        market=body.market,
        name=body.name,
        quantity=body.quantity,
        amount=body.amount,
        price=body.price,
    )
    if not result.get("ok", True):
        return JSONResponse(status_code=400, content=result)
    return result


@app.post("/api/stock/simulator/sell")
async def api_simulator_sell(body: SimulatorSellRequest):
    from src.core.stock_simulator_service import get_stock_simulator_service

    result = await run_in_threadpool(
        get_stock_simulator_service().sell,
        account_id=body.account_id,
        code=body.code,
        market=body.market,
        quantity=body.quantity,
        price=body.price,
    )
    if not result.get("ok", True):
        return JSONResponse(status_code=400, content=result)
    return result


@app.put("/api/stock/simulator/rules")
async def api_simulator_update_rules(body: SimulatorRulesRequest):
    from src.core.stock_simulator_service import get_stock_simulator_service

    rules = body.model_dump(exclude={"account_id"}, exclude_none=True)
    result = await run_in_threadpool(
        get_stock_simulator_service().update_rules,
        body.account_id,
        rules,
    )
    return result


@app.post("/api/stock/simulator/evaluate")
async def api_simulator_evaluate(account_id: str = Query("default")):
    from src.core.stock_simulator_service import get_stock_simulator_service

    result = await run_in_threadpool(get_stock_simulator_service().evaluate, account_id)
    return result


@app.get("/api/stock/simulator/trades")
async def api_simulator_trades(
    account_id: str = Query("default"),
    limit: int = Query(50, ge=1, le=200),
):
    from src.core.stock_simulator_service import get_stock_simulator_service

    result = await run_in_threadpool(
        get_stock_simulator_service().list_trades,
        account_id,
        limit,
    )
    return result


def _universe_pick_kwargs(body: SimulatorBacktestRequest | SimulatorPickStocksRequest) -> dict[str, Any]:
    uni = body.universe or SimulatorUniverseConfig()
    kwargs: dict[str, Any] = {
        "universe": uni.universe,
        "classify": uni.classify,
        "symbol": uni.symbol,
        "code": uni.code,
        "limit": uni.limit,
        "strategy": getattr(body, "pick_strategy", "rules"),
        "start_date": getattr(body, "start_date", None),
        "end_date": getattr(body, "end_date", None),
        "momentum_lookback": getattr(body, "momentum_lookback", 5),
    }
    if isinstance(body, SimulatorPickStocksRequest):
        kwargs["constraints"] = body.constraints
    elif isinstance(body, SimulatorBacktestRequest):
        kwargs["constraints"] = body.pick_constraints
    return kwargs


@app.get("/api/stock/simulator/strategies")
async def api_simulator_strategies():
    from src.core.stock_simulator_stock_picker import list_pick_strategies

    return list_pick_strategies()


@app.post("/api/stock/simulator/pick-stocks")
async def api_simulator_pick_stocks(body: SimulatorPickStocksRequest):
    """自动选股：支持经典策略（动量/GARP/双均线/行业轮动等）与 Agent，返回 {code,name,market,reason}。"""
    from src.core.stock_simulator_stock_picker import pick_stocks

    kwargs = _universe_pick_kwargs(body)
    kwargs["as_of_date"] = (body.as_of_date or body.start_date or "")[:10] or None
    result = await run_in_threadpool(pick_stocks, **kwargs)
    if not result.get("ok"):
        return JSONResponse(status_code=400, content=result)
    return result


@app.post("/api/stock/simulator/pick")
async def api_simulator_pick(body: SimulatorPickStocksRequest):
    from src.core.stock_simulator_stock_picker import pick_stocks

    u = _simulator_universe_config(body.universe)
    as_of = (body.as_of_date or body.start_date or "")[:10] or None
    result = await run_in_threadpool(
        pick_stocks,
        universe=u.universe,
        classify=u.classify,
        symbol=u.symbol,
        code=u.code,
        limit=u.limit,
        strategy=body.pick_strategy,
        start_date=body.start_date,
        end_date=body.end_date,
        as_of_date=as_of,
        constraints=body.constraints,
        momentum_lookback=body.momentum_lookback,
    )
    if not result.get("ok"):
        return JSONResponse(status_code=400, content=result)
    return result


@app.get("/api/stock/simulator/pick")
async def api_simulator_pick_preview(
    pick_strategy: str = Query("rules"),
    universe: str = Query("sector"),
    classify: str = Query("concept"),
    symbol: str = Query(""),
    code: str | None = Query(None),
    limit: int = Query(5, ge=1, le=20),
    as_of_date: str | None = Query(None, description="决策时点，仅使用 date<=as_of_date 的数据"),
    start_date: str | None = Query(None),
    momentum_lookback: int = Query(5, ge=5, le=60),
    pe_max: float | None = Query(None),
    pb_max: float | None = Query(None),
    roe_min: float | None = Query(None),
    peg_max: float | None = Query(None),
):
    from src.core.stock_simulator_stock_picker import pick_stocks

    pit = (as_of_date or start_date or "")[:10] or None
    constraints: dict[str, Any] = {}
    if pe_max is not None:
        constraints["pe_max"] = pe_max
    if pb_max is not None:
        constraints["pb_max"] = pb_max
    if roe_min is not None:
        constraints["roe_min"] = roe_min
    if peg_max is not None:
        constraints["peg_max"] = peg_max
    result = await run_in_threadpool(
        pick_stocks,
        universe=universe,
        classify=classify,
        symbol=symbol,
        code=code,
        limit=limit,
        strategy=pick_strategy,
        start_date=start_date,
        as_of_date=pit,
        momentum_lookback=momentum_lookback,
        constraints=constraints or None,
    )
    if not result.get("ok"):
        return JSONResponse(status_code=400, content=result)
    return result


def _simulator_backtest_run_kwargs(body: SimulatorBacktestRequest) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"rebalance_interval": body.rebalance_interval}
    if body.auto_pick:
        kwargs["auto_pick"] = True
        kwargs["pick_kwargs"] = _simulator_pick_kwargs(
            pick_strategy=body.pick_strategy,
            universe=body.universe,
            start_date=body.start_date,
            end_date=body.end_date,
            constraints=body.pick_constraints,
            momentum_lookback=body.momentum_lookback,
        )
    return kwargs


@app.post("/api/stock/simulator/backtest")
async def api_simulator_backtest(body: SimulatorBacktestRequest):
    import uuid

    from src.core.stock_simulator_backtest import get_stock_simulator_backtest_service

    run_id = str(uuid.uuid4())
    bt_kwargs = _simulator_backtest_run_kwargs(body)

    if body.async_run:
        import asyncio

        async def _run():
            await run_in_threadpool(
                get_stock_simulator_backtest_service().run_backtest,
                codes=body.codes,
                start_date=body.start_date,
                end_date=body.end_date,
                initial_cash=body.initial_cash,
                strategy=body.strategy,
                rules=body.rules,
                run_id=run_id,
                **bt_kwargs,
            )

        asyncio.create_task(_run())
        return {"ok": True, "run_id": run_id, "status": "running", "message": "回测已提交，请轮询结果"}

    result = await run_in_threadpool(
        get_stock_simulator_backtest_service().run_backtest,
        codes=body.codes,
        start_date=body.start_date,
        end_date=body.end_date,
        initial_cash=body.initial_cash,
        strategy=body.strategy,
        rules=body.rules,
        run_id=run_id,
        **bt_kwargs,
    )
    if not result.get("ok"):
        return JSONResponse(status_code=400, content=result)
    return result


@app.post("/api/stock/simulator/backtest/agent")
async def api_simulator_backtest_agent(body: SimulatorBacktestAgentRequest):
    import uuid

    from src.core.stock_simulator_backtest import get_stock_simulator_backtest_service

    run_id = str(uuid.uuid4())
    bt_kwargs = _simulator_backtest_run_kwargs(body)

    if body.async_run:
        import asyncio

        async def _run():
            await run_in_threadpool(
                get_stock_simulator_backtest_service().run_backtest,
                codes=body.codes,
                start_date=body.start_date,
                end_date=body.end_date,
                initial_cash=body.initial_cash,
                strategy="agent",
                rules=body.rules,
                run_id=run_id,
                agent_interval=body.agent_interval,
                lookback_days=body.lookback_days,
                timeout=body.timeout,
                **bt_kwargs,
            )

        asyncio.create_task(_run())
        return {"ok": True, "run_id": run_id, "status": "running", "message": "Agent 回测已提交"}

    result = await run_in_threadpool(
        get_stock_simulator_backtest_service().run_backtest,
        codes=body.codes,
        start_date=body.start_date,
        end_date=body.end_date,
        initial_cash=body.initial_cash,
        strategy="agent",
        rules=body.rules,
        run_id=run_id,
        agent_interval=body.agent_interval,
        lookback_days=body.lookback_days,
        timeout=body.timeout,
        **bt_kwargs,
    )
    if not result.get("ok"):
        return JSONResponse(status_code=400, content=result)
    return result


@app.get("/api/stock/simulator/backtest/{run_id}")
async def api_simulator_backtest_result(run_id: str):
    from src.core.stock_simulator_backtest import get_stock_simulator_backtest_service

    result = await run_in_threadpool(get_stock_simulator_backtest_service().get_run, run_id)
    if result is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "回测结果不存在或仍在运行", "run_id": run_id})
    return result
