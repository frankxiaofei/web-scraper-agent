"""Chrome 插件录制 WebSocket 消息处理。"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.web.crawl_recorder_service import get_crawl_recorder_service
from src.web.crawl_recorder_ws import get_recorder_ws_manager

logger = logging.getLogger(__name__)


async def _broadcast_session_state(session_id: str) -> None:
    svc = get_crawl_recorder_service()
    ws_mgr = get_recorder_ws_manager()
    result = svc.get_session(session_id)
    if not result.get("ok"):
        return
    await ws_mgr.send_to_page(
        session_id,
        {
            "type": "session_state",
            "session": result["session"],
            "connections": ws_mgr.connection_status(session_id),
        },
    )


async def _push_workflow_snapshot(
    session_id: str,
    *,
    partial: bool = False,
) -> Optional[dict[str, Any]]:
    svc = get_crawl_recorder_service()
    ws_mgr = get_recorder_ws_manager()
    compiled = svc.compile_session(session_id)
    if not compiled.get("ok"):
        return None
    await ws_mgr.send_to_page(
        session_id,
        {
            "type": "workflow_snapshot",
            "partial": partial,
            "workflow": compiled["workflow"],
            "step_count": compiled["step_count"],
            "session": compiled["session"],
        },
    )
    return compiled


async def handle_recorder_ws_message(
    session_id: str,
    role: str,
    payload: dict[str, Any],
) -> None:
    """处理客户端 WebSocket 消息并广播状态。"""
    svc = get_crawl_recorder_service()
    ws_mgr = get_recorder_ws_manager()
    msg_type = payload.get("type") or payload.get("message_type") or ""

    if msg_type == "ping":
        target = ws_mgr.send_to_page if role == "page" else ws_mgr.send_to_extension
        await target(session_id, {"type": "pong", "ts": payload.get("ts")})
        return

    if msg_type == "workflow_ready" and role == "page":
        session = svc.mark_page_ready(session_id)
        if session:
            await ws_mgr.send_to_page(
                session_id,
                {
                    "type": "workflow_ready",
                    "session": session.to_public_dict(),
                    "connections": ws_mgr.connection_status(session_id),
                },
            )
            if ws_mgr.has_extension(session_id):
                ext_session = svc.get_session(session_id)
                if ext_session.get("ok"):
                    await ws_mgr.send_to_page(
                        session_id,
                        {
                            "type": "extension_ready",
                            "version": session.extension_version,
                            "session": ext_session["session"],
                        },
                    )
        return

    if msg_type == "extension_ready" and role == "extension":
        version = str(payload.get("version") or "")
        session = svc.mark_extension_ready(session_id, version=version)
        if session:
            pub = session.to_public_dict()
            await ws_mgr.send_to_page(
                session_id,
                {
                    "type": "extension_ready",
                    "version": version,
                    "session": pub,
                },
            )
            await ws_mgr.send_to_extension(
                session_id,
                {
                    "type": "session_ack",
                    "session_id": session_id,
                    "page_connected": ws_mgr.has_page(session_id),
                },
            )
        return

    if msg_type == "step_recorded" and role == "extension":
        step = payload.get("step")
        if not isinstance(step, dict):
            return
        result = svc.append_step(session_id, step)
        if not result.get("ok"):
            await ws_mgr.send_to_extension(
                session_id,
                {"type": "error", "error": result.get("error") or "追加步骤失败"},
            )
            return
        session = result["session"]
        steps = session.get("steps") or []
        await ws_mgr.send_to_page(
            session_id,
            {
                "type": "step_recorded",
                "step": step,
                "step_index": len(steps),
                "session": session,
            },
        )
        await _push_workflow_snapshot(session_id, partial=True)
        return

    if msg_type == "list_page_detected" and role == "extension":
        await ws_mgr.send_to_page(
            session_id,
            {
                "type": "list_page_detected",
                "url": payload.get("url") or "",
                "hints": payload.get("hints") or {},
            },
        )
        return

    if msg_type == "list_page_confirmed" and role == "extension":
        analyze_result = svc.analyze_session(
            session_id,
            html=str(payload.get("html") or ""),
            url=str(payload.get("url") or ""),
            confirmed_list_page=True,
            list_hints=payload.get("list_hints"),
            pagination=payload.get("pagination"),
        )
        if analyze_result.get("ok"):
            await ws_mgr.send_to_page(
                session_id,
                {
                    "type": "list_page_confirmed",
                    "url": payload.get("url") or "",
                    "analysis": analyze_result.get("analysis"),
                    "session": analyze_result.get("session"),
                },
            )
            await _push_workflow_snapshot(session_id, partial=True)
        return

    if msg_type == "recording_stopped" and role == "extension":
        session = svc.mark_recording_stopped(session_id)
        if not session:
            return
        pub = session.to_public_dict()
        await ws_mgr.send_to_page(
            session_id,
            {
                "type": "recording_stopped",
                "session": pub,
            },
        )
        await _push_workflow_snapshot(session_id, partial=False)
        await ws_mgr.send_to_extension(
            session_id,
            {"type": "recording_stopped_ack", "session_id": session_id},
        )
        return

    logger.debug("ignored ws message session=%s role=%s type=%s", session_id, role, msg_type)
