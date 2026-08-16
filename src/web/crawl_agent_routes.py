"""Crawl Agent / Hermes 对话路由 — 供主 UI (8090) 与农业 UI (8091) 复用。"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from src.core.config import get_settings
from src.core.site_sync import get_site_by_id
from src.web.data_service import get_data_service

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SKILLS_DIR = PROJECT_ROOT / "skills"

RenderFn = Callable[[str, dict[str, Any]], HTMLResponse]


class CreatePendingRequest(BaseModel):
    site_id: str
    input_type: Literal["login_cookie", "captcha", "rule_confirm", "generic"] = "generic"
    prompt: str
    choices: Optional[list[str]] = None
    context: Optional[dict[str, Any]] = None


class ResolvePendingRequest(BaseModel):
    user_response: str


class CreateActionRequest(BaseModel):
    site_id: str
    action: str
    detail: str = ""
    source: Literal["hermes", "web", "daemon"] = "web"


class CrawlAgentSkillExecuteRequest(BaseModel):
    tool: str = ""
    skill: str = ""
    arguments: dict[str, Any] = {}
    args: dict[str, Any] = {}


class CrawlAgentExecuteCodeRequest(BaseModel):
    mode: Literal["script", "code"]
    script: Optional[str] = None
    args: Any = None
    code: Optional[str] = None
    label: Optional[str] = None
    timeout_seconds: Optional[int] = None


class CrawlAgentChatMessage(BaseModel):
    role: Literal["user", "assistant"] = "user"
    content: str


class CrawlAgentChatRequest(BaseModel):
    message: str
    history: list[CrawlAgentChatMessage] = []
    session_id: Optional[str] = None
    agent_profile: Optional[str] = None


class CreateChatSessionRequest(BaseModel):
    title: str = ""


class CrawlAgentCancelRequest(BaseModel):
    session_id: str


def list_project_skills() -> list[dict[str, str]]:
    """扫描 skills/*/SKILL.md，供 Hermes 对话页展示可用 Skill 文档。"""
    if not SKILLS_DIR.is_dir():
        return []
    items: list[dict[str, str]] = []
    for path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        skill_id = path.parent.name
        items.append({"id": skill_id, "path": f"skills/{skill_id}/SKILL.md"})
    return items


def _execute_crawl_agent_tool(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from src.web.crawl_agent_tools import CrawlAgentToolExecutor

    executor = CrawlAgentToolExecutor()
    result, _ = executor.execute(tool, arguments)
    ok = result.get("ok") is not False
    payload: dict[str, Any] = {"ok": ok, "result": result}
    err = result.get("error")
    # 成功响应勿带 "error": null — Hermes _detect_tool_failure 会把含 "error" 键的 JSON 判为失败。
    if err:
        payload["error"] = err
    return payload


def _resolve_agent_profile(
    explicit: Optional[str],
    *,
    default: str = "default",
) -> str:
    profile = (explicit or default or "default").strip().lower()
    if profile == "agri" or os.getenv("AGRI_AGENT", "").strip() in ("1", "true", "yes"):
        return "agri"
    if profile == "stock" or os.getenv("STOCK_AGENT", "").strip() in ("1", "true", "yes"):
        return "stock"
    return profile if profile in ("default", "agri", "stock") else "default"


def _default_chat_page_context(*, agent_profile: str = "default") -> dict[str, Any]:
    svc = get_data_service()
    settings = get_settings()
    from src.core.hermes_agent_chat_client import (
        get_hermes_agent_chat_client,
        resolve_hermes_agent_chat_url,
    )
    from src.web.crawl_agent_tools import resolve_tool_schemas

    hermes_client = get_hermes_agent_chat_client()
    hermes_chat_url = resolve_hermes_agent_chat_url(settings)
    profile_tools = resolve_tool_schemas(agent_profile)
    if agent_profile == "agri":
        hermes_theme = "agri"
    elif agent_profile == "stock":
        hermes_theme = "stock"
    else:
        hermes_theme = "violet"
    ctx: dict[str, Any] = {
        "data_source": svc.data_source,
        "hermes_agent_configured": hermes_client.available,
        "hermes_agent_chat_url": hermes_chat_url,
        "llm_configured": hermes_client.available,
        "llm_model": "hermes-agent",
        "active_nav": "hermes",
        "project_skills": list_project_skills(),
        "default_tool_count": len(profile_tools),
        "mcp_enabled": False,
        "hermes_theme": hermes_theme,
        "agent_profile": agent_profile,
        "sse_reconnect_max": settings.sse_reconnect_max,
    }
    if agent_profile == "agri":
        from src.web.agri_insights_service import get_agri_ui_port, get_main_ui_port

        ctx["main_ui_port"] = get_main_ui_port()
        ctx["agri_ui_port"] = get_agri_ui_port()
        ctx["days"] = 7
    elif agent_profile == "stock":
        from src.web.stock_insights_service import (
            get_agri_ui_port,
            get_main_ui_port,
            get_stock_ui_port,
        )

        ctx["main_ui_port"] = get_main_ui_port()
        ctx["agri_ui_port"] = get_agri_ui_port()
        ctx["stock_ui_port"] = get_stock_ui_port()
        ctx["days"] = 7
    return ctx


def register_crawl_agent_routes(
    app: FastAPI,
    *,
    render: RenderFn,
    default_agent_profile: str = "default",
    chat_template: str = "crawl_agent_chat.html",
    include_chat_alias: bool = True,
    include_pending_page: bool = True,
    format_pending_for_ui: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> None:
    """在 FastAPI 应用上注册 Crawl Agent / Hermes 对话相关路由。"""

    @app.post("/api/crawl-agent/skills/execute")
    async def api_execute_crawl_agent_skill(body: CrawlAgentSkillExecuteRequest):
        tool = (body.tool or body.skill or "").strip()
        if not tool:
            raise HTTPException(status_code=400, detail="tool 不能为空")
        arguments = body.arguments or body.args or {}
        return _execute_crawl_agent_tool(tool, arguments)

    @app.post("/api/crawl-agent/skills/{tool_name}")
    async def api_execute_crawl_agent_skill_by_name(
        tool_name: str,
        body: dict[str, Any] = Body(default_factory=dict),
    ):
        tool = (tool_name or "").strip()
        if not tool:
            raise HTTPException(status_code=400, detail="tool_name 不能为空")
        return _execute_crawl_agent_tool(tool, body or {})

    @app.post("/api/crawl-agent/execute-code")
    async def api_crawl_agent_execute_code(body: CrawlAgentExecuteCodeRequest):
        from src.core.code_executor import execute_code

        result = execute_code(
            body.mode,
            script=(body.script or "").strip() or None,
            args=body.args,
            code=body.code,
            label=(body.label or "").strip() or None,
            timeout_seconds=body.timeout_seconds,
        )
        ok = result.get("ok") is True
        return {"ok": ok, "result": result, "error": result.get("error")}

    if include_pending_page:

        @app.get("/crawl-agent/pending", response_class=HTMLResponse)
        async def crawl_agent_pending_page(request: Request):
            from src.core.crawl_agent_pending import list_pending_items

            svc = get_data_service()
            raw = list_pending_items(status="pending")
            items = format_pending_for_ui(raw) if format_pending_for_ui else raw
            return render(
                "crawl_agent_pending.html",
                {
                    "request": request,
                    "data_source": svc.data_source,
                    "items": items,
                    "count": len(items),
                },
            )

    @app.get("/api/crawl-agent/pending")
    async def api_list_crawl_agent_pending(
        status: str = Query("pending", pattern="^(pending|resolved|all)$"),
        site_id: Optional[str] = Query(None),
    ):
        from src.core.crawl_agent_pending import list_pending_items

        items = list_pending_items(status=status, site_id=site_id)
        return {"items": items, "count": len(items)}

    @app.post("/api/crawl-agent/pending")
    async def api_create_crawl_agent_pending(body: CreatePendingRequest):
        from src.core.crawl_agent_pending import create_pending_item

        site = get_site_by_id(body.site_id)
        if not site:
            raise HTTPException(status_code=404, detail="站点不存在")
        try:
            item = create_pending_item(
                site_id=body.site_id,
                input_type=body.input_type,
                prompt=body.prompt,
                choices=body.choices,
                context=body.context,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "item": item}

    @app.post("/api/crawl-agent/pending/{item_id}/resolve")
    async def api_resolve_crawl_agent_pending(item_id: str, body: ResolvePendingRequest):
        from src.core.crawl_agent_pending import resolve_pending_item

        try:
            return resolve_pending_item(item_id, body.user_response)
        except LookupError:
            raise HTTPException(status_code=404, detail="待办项不存在")
        except ValueError as exc:
            msg = str(exc)
            status = 409 if "已处理" in msg else 400
            raise HTTPException(status_code=status, detail=msg)

    @app.get("/api/crawl-agent/actions")
    async def api_list_crawl_agent_actions(
        site_id: Optional[str] = Query(None),
        limit: int = Query(50, ge=1, le=500),
    ):
        from src.core.crawl_agent_actions import enrich_action, list_actions

        items = [enrich_action(r) for r in list_actions(site_id=site_id, limit=limit)]
        return {"items": items, "count": len(items)}

    @app.post("/api/crawl-agent/actions")
    async def api_create_crawl_agent_action(body: CreateActionRequest):
        from src.core.crawl_agent_actions import append_action, enrich_action

        site = get_site_by_id(body.site_id)
        if not site:
            raise HTTPException(status_code=404, detail="站点不存在")
        try:
            record = append_action(
                site_id=body.site_id,
                action=body.action,
                detail=body.detail,
                source=body.source,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "item": enrich_action(record)}

    async def crawl_agent_chat_page(request: Request, *, extra_context: dict[str, Any] | None = None):
        ctx = _default_chat_page_context(agent_profile=default_agent_profile)
        if extra_context:
            ctx.update(extra_context)
        ctx["request"] = request
        return render(chat_template, ctx)

    @app.get("/hermes", response_class=HTMLResponse)
    async def hermes_chat_page(request: Request):
        return await crawl_agent_chat_page(request)

    if include_chat_alias:

        @app.get("/crawl-agent/chat", response_class=HTMLResponse)
        async def crawl_agent_chat_alias(request: Request):
            return await crawl_agent_chat_page(request)

    @app.get("/api/crawl-agent/chats")
    async def api_list_crawl_agent_chats(limit: int = Query(50, ge=1, le=200)):
        from src.core.crawl_agent_chat_store import list_sessions

        items = list_sessions(limit=limit, agent_profile=default_agent_profile)
        return {"items": items, "count": len(items)}

    @app.get("/api/crawl-agent/chats/{session_id}")
    async def api_get_crawl_agent_chat(session_id: str):
        from src.core.config import get_settings
        from src.core.crawl_agent_chat_store import get_session
        from src.web.crawl_agent_run_registry import get_run_info
        from src.web.crawl_agent_stream_reconcile import (
            count_buffered_events,
            reconcile_streaming_session,
        )

        session = get_session(session_id, agent_profile=default_agent_profile)
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")

        settings = get_settings()
        session = reconcile_streaming_session(
            session,
            agent_profile=default_agent_profile,
            stale_minutes=settings.crawl_agent_stream_stale_minutes,
        )

        run = get_run_info(session_id)
        session = dict(session)
        if run:
            session["run"] = run
        elif session.get("streaming"):
            session["run"] = {
                "status": "interrupted",
                "event_count": count_buffered_events(session),
            }
        return session

    @app.post("/api/crawl-agent/chats")
    async def api_create_crawl_agent_chat(
        body: CreateChatSessionRequest = Body(default_factory=CreateChatSessionRequest),
    ):
        from src.core.crawl_agent_chat_store import create_session, summarize_session

        session = create_session(title=body.title, agent_profile=default_agent_profile)
        return {"ok": True, "session": summarize_session(session)}

    @app.delete("/api/crawl-agent/chats")
    async def api_delete_all_crawl_agent_chats():
        from src.core.crawl_agent_chat_store import delete_all_sessions

        deleted = delete_all_sessions(agent_profile=default_agent_profile)
        return {"ok": True, "deleted": deleted}

    @app.delete("/api/crawl-agent/chats/{session_id}")
    async def api_delete_crawl_agent_chat(session_id: str):
        from src.core.crawl_agent_chat_store import delete_session
        from src.web.crawl_agent_run_registry import clear_run

        if not delete_session(session_id, agent_profile=default_agent_profile):
            raise HTTPException(status_code=404, detail="会话不存在")
        clear_run(session_id)
        return {"ok": True}

    @app.post("/api/crawl-agent/chat/{session_id}/cancel")
    async def api_cancel_crawl_agent_chat_by_path(session_id: str):
        from src.web.crawl_agent_cancel import cancel_chat_session

        return cancel_chat_session(session_id.strip())

    @app.post("/api/crawl-agent/cancel")
    async def api_cancel_crawl_agent_chat(body: CrawlAgentCancelRequest):
        from src.web.crawl_agent_cancel import cancel_chat_session

        session_id = (body.session_id or "").strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id 不能为空")
        return cancel_chat_session(session_id)

    @app.post("/api/crawl-agent/chat")
    async def api_crawl_agent_chat(body: CrawlAgentChatRequest, request: Request):
        from src.core.crawl_agent_chat_store import (
            abort_streaming_turn,
            begin_streaming_turn,
            create_session,
            get_session,
            messages_to_history,
        )
        from src.web.crawl_agent_cancel import register_chat_session
        from src.web.crawl_agent_run_executor import spawn_chat_run
        from src.web.crawl_agent_run_registry import (
            is_run_active,
            start_run,
            stream_run_events,
        )
        from src.web import sse_utils
        from src.web.sse_utils import SSE_STREAM_HEADERS, format_sse_data, iter_with_idle_keepalive

        message = (body.message or "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="message 不能为空")

        from src.billing.quota_middleware import consume_entitlement, tenant_from_request

        consume_entitlement(tenant_from_request(request), "quota.hermes_messages")

        agent_profile = _resolve_agent_profile(
            body.agent_profile,
            default=default_agent_profile,
        )

        session_id = (body.session_id or "").strip() or None
        if session_id:
            session = get_session(session_id, agent_profile=agent_profile)
            if not session:
                raise HTTPException(status_code=404, detail="会话不存在")
            if is_run_active(session_id):
                raise HTTPException(status_code=409, detail="该会话已有进行中的 Agent 任务")
            if session.get("streaming"):
                abort_streaming_turn(session_id, agent_profile=agent_profile)
            history = messages_to_history(session.get("messages") or [])
        else:
            session = create_session(agent_profile=agent_profile)
            session_id = session["session_id"]
            history = [{"role": m.role, "content": m.content} for m in body.history]

        try:
            begin_streaming_turn(session_id, user_message=message, agent_profile=agent_profile)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        if not start_run(session_id, message):
            abort_streaming_turn(session_id, agent_profile=agent_profile)
            raise HTTPException(status_code=409, detail="该会话已有进行中的 Agent 任务")

        cancel_event = register_chat_session(session_id)
        spawn_chat_run(
            session_id=session_id,
            message=message,
            history=history,
            agent_profile=agent_profile,
            cancel_event=cancel_event,
        )

        async def event_generator():
            session_payload = json.dumps({"type": "session", "session_id": session_id}, ensure_ascii=False)
            yield f"data: {session_payload}\n\n"
            async for item in iter_with_idle_keepalive(
                stream_run_events(session_id, after=0),
                interval=sse_utils.SSE_KEEPALIVE_INTERVAL,
            ):
                if isinstance(item, str):
                    yield item
                    continue
                yield format_sse_data(item)

        headers = {**SSE_STREAM_HEADERS, "X-Session-Id": session_id}
        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers=headers,
        )

    @app.get("/api/crawl-agent/chats/{session_id}/stream")
    async def api_reconnect_crawl_agent_stream(
        session_id: str,
        after: int = Query(0, ge=0),
    ):
        from src.core.config import get_settings
        from src.core.crawl_agent_chat_store import get_session
        from src.web.crawl_agent_run_registry import get_run_info, stream_run_events
        from src.web.crawl_agent_stream_reconcile import (
            count_buffered_events,
            reconcile_streaming_session,
        )
        from src.web.sse_utils import SSE_STREAM_HEADERS, format_sse_data, iter_with_idle_keepalive

        session = get_session(session_id, agent_profile=default_agent_profile)
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")

        settings = get_settings()
        reconcile_streaming_session(
            session,
            agent_profile=default_agent_profile,
            stale_minutes=settings.crawl_agent_stream_stale_minutes,
        )

        run = get_run_info(session_id)
        if run is None or run.get("status") != "running":
            raise HTTPException(status_code=404, detail="无进行中的 Agent 任务")

        async def event_generator():
            from src.web import sse_utils

            async for event in iter_with_idle_keepalive(
                stream_run_events(session_id, after=after),
                interval=sse_utils.SSE_KEEPALIVE_INTERVAL,
            ):
                if isinstance(event, str):
                    yield event
                    continue
                yield format_sse_data(event)

        headers = {**SSE_STREAM_HEADERS, "X-Session-Id": session_id}
        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers=headers,
        )
