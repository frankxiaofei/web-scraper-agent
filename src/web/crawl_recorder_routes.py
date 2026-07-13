"""Chrome 插件爬取工作流录制 API 路由。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.web.crawl_recorder_service import EXTENSION_DIR, get_crawl_recorder_service
from src.web.crawl_recorder_ws import get_recorder_ws_manager
from src.web.crawl_recorder_ws_handler import handle_recorder_ws_message

router = APIRouter(prefix="/api/crawl-recorder", tags=["crawl-recorder"])


class CreateSessionRequest(BaseModel):
    site_id: str
    entry_url: str = ""


class AppendStepsRequest(BaseModel):
    steps: list[dict[str, Any]] = Field(default_factory=list)


class AnalyzeSessionRequest(BaseModel):
    html: str = ""
    url: str = ""
    confirmed_list_page: bool = False
    list_hints: Optional[dict[str, Any]] = None
    pagination: Optional[dict[str, Any]] = None


class ImportSessionRequest(BaseModel):
    save: bool = False


class PingRequest(BaseModel):
    source: str = "extension"


@router.get("/status")
async def recorder_status():
    svc = get_crawl_recorder_service()
    return {"ok": True, "service": "crawl-recorder", "extension": svc.get_extension_info()}


@router.get("/extension")
async def recorder_extension_info():
    return get_crawl_recorder_service().get_extension_info()


@router.post("/sessions")
async def create_recorder_session(body: CreateSessionRequest):
    result = get_crawl_recorder_service().create_session(
        site_id=body.site_id,
        entry_url=body.entry_url,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "创建失败"))
    return result


@router.get("/sessions/waiting/list")
async def list_waiting_recorder_sessions(site_id: str = ""):
    """扩展轮询：发现 workflow 页已创建、等待插件连接的会话。"""
    return get_crawl_recorder_service().list_waiting_sessions(site_id=site_id)


@router.get("/sessions/{session_id}")
async def get_recorder_session(session_id: str):
    result = get_crawl_recorder_service().get_session(session_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "会话不存在"))
    return result


@router.post("/sessions/{session_id}/ping")
async def ping_recorder_session(
    session_id: str,
    body: PingRequest = Body(default_factory=PingRequest),
):
    result = get_crawl_recorder_service().ping_session(session_id, source=body.source)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "会话不存在"))
    return result


@router.post("/sessions/{session_id}/steps")
async def append_recorder_steps(session_id: str, body: AppendStepsRequest):
    result = get_crawl_recorder_service().append_steps(session_id, body.steps)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "追加失败"))
    return result


@router.post("/sessions/{session_id}/analyze")
async def analyze_recorder_session(session_id: str, body: AnalyzeSessionRequest = Body(default_factory=AnalyzeSessionRequest)):
    result = get_crawl_recorder_service().analyze_session(
        session_id,
        html=body.html,
        url=body.url,
        confirmed_list_page=body.confirmed_list_page,
        list_hints=body.list_hints,
        pagination=body.pagination,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "分析失败"))
    return result


@router.post("/sessions/{session_id}/compile")
async def compile_recorder_session(session_id: str):
    result = get_crawl_recorder_service().compile_session(session_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "编译失败"))
    return result


@router.post("/sessions/{session_id}/import")
async def import_recorder_session(
    session_id: str,
    body: ImportSessionRequest = Body(default_factory=ImportSessionRequest),
):
    result = get_crawl_recorder_service().import_session(session_id, save=body.save)
    if not result.get("ok"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error") or result.get("save_result") or "导入失败",
        )
    return result


@router.get("/files/{file_path:path}")
async def serve_extension_file(file_path: str):
    """供参考下载扩展内静态文件（主安装方式仍为 load unpacked）。"""
    base = EXTENSION_DIR.resolve()
    target = (base / file_path).resolve()
    if not str(target).startswith(str(base)) or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target)


@router.websocket("/sessions/{session_id}/ws")
async def recorder_websocket(
    websocket: WebSocket,
    session_id: str,
    client: str = Query(default="page", pattern="^(page|extension)$"),
):
    """录制会话实时同步 WebSocket。client=page|extension"""
    svc = get_crawl_recorder_service()
    ws_mgr = get_recorder_ws_manager()

    session = svc.get_session(session_id)
    if not session.get("ok"):
        await websocket.close(code=4404, reason="会话不存在或已过期")
        return

    await websocket.accept()
    await ws_mgr.register(session_id, websocket, client)

    try:
        if client == "page":
            svc.mark_page_ready(session_id)
            await ws_mgr.send_to_page(
                session_id,
                {
                    "type": "connected",
                    "role": "page",
                    "session": session["session"],
                    "connections": ws_mgr.connection_status(session_id),
                },
            )
            if ws_mgr.has_extension(session_id):
                ext = svc.get_session(session_id)
                if ext.get("ok"):
                    await ws_mgr.send_to_page(
                        session_id,
                        {
                            "type": "extension_ready",
                            "version": ext["session"].get("extension_version") or "",
                            "session": ext["session"],
                        },
                    )
        else:
            await ws_mgr.send_to_extension(
                session_id,
                {
                    "type": "connected",
                    "role": "extension",
                    "session_id": session_id,
                    "page_connected": ws_mgr.has_page(session_id),
                },
            )

        while True:
            payload = await websocket.receive_json()
            if not isinstance(payload, dict):
                continue
            await handle_recorder_ws_message(session_id, client, payload)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await ws_mgr.unregister(session_id, client)
        svc.mark_client_disconnected(session_id, role=client)
