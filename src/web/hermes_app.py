"""Hermes Crawl Agent 独立 FastAPI 应用 — Docker `hermes` 容器专用。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Hermes Crawl Agent", description="爬虫运维对话与 crawl-agent API")
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

_jinja_env = Environment(
    loader=FileSystemLoader(WEB_DIR / "templates"),
    autoescape=select_autoescape(["html", "xml"]),
)
_jinja_env.filters["urlencode"] = lambda s: quote_plus(str(s))


def _render(name: str, context: dict[str, Any], *, status_code: int = 200) -> HTMLResponse:
    template = _jinja_env.get_template(name)
    return HTMLResponse(template.render(**context), status_code=status_code)


@app.on_event("startup")
async def _detect_stale_sync_on_startup() -> None:
    from src.web.sync_service import get_sync_service

    sync_svc = get_sync_service()
    stale = sync_svc.detect_stale_syncs()
    if stale:
        logger.warning("检测到 %d 个 stale syncing 状态（服务重启后内存任务丢失）: %s", len(stale), stale)


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/hermes")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "hermes"}


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
