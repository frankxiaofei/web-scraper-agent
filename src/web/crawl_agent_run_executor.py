"""Hermes Crawl Agent 后台 run 执行器 — 与 SSE 连接解耦，刷新页面不中断任务。"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_tasks: dict[str, asyncio.Task[None]] = {}


def is_background_running(session_id: str) -> bool:
    with _lock:
        task = _tasks.get(session_id)
        return task is not None and not task.done()


def spawn_chat_run(
    *,
    session_id: str,
    message: str,
    history: list[dict[str, str]],
    agent_profile: str,
    cancel_event: threading.Event,
) -> None:
    """启动后台 chat run（同 session 已有任务在跑则跳过）。"""
    with _lock:
        existing = _tasks.get(session_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            _run_chat_background(
                session_id=session_id,
                message=message,
                history=history,
                agent_profile=agent_profile,
                cancel_event=cancel_event,
            ),
            name=f"crawl-agent-run:{session_id[:8]}",
        )
        _tasks[session_id] = task

        def _done_cb(t: asyncio.Task[None]) -> None:
            with _lock:
                if _tasks.get(session_id) is t:
                    _tasks.pop(session_id, None)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.exception("后台 chat run 异常 session=%s", session_id, exc_info=exc)

        task.add_done_callback(_done_cb)


def spawn_hermes_resume(
    *,
    session_id: str,
    hermes_run_id: str,
    agent_profile: str,
    cancel_event: threading.Event,
) -> None:
    """服务重启后从 Hermes run_id 恢复事件 relay。"""
    with _lock:
        existing = _tasks.get(session_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            _resume_hermes_background(
                session_id=session_id,
                hermes_run_id=hermes_run_id,
                agent_profile=agent_profile,
                cancel_event=cancel_event,
            ),
            name=f"crawl-agent-resume:{session_id[:8]}",
        )
        _tasks[session_id] = task

        def _done_cb(t: asyncio.Task[None]) -> None:
            with _lock:
                if _tasks.get(session_id) is t:
                    _tasks.pop(session_id, None)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.exception("Hermes resume 异常 session=%s", session_id, exc_info=exc)

        task.add_done_callback(_done_cb)


async def _run_chat_background(
    *,
    session_id: str,
    message: str,
    history: list[dict[str, str]],
    agent_profile: str,
    cancel_event: threading.Event,
) -> None:
    from src.core.crawl_agent_chat_store import (
        finalize_streaming_turn,
        set_active_hermes_run_id,
        update_streaming_turn,
    )
    from src.web.crawl_agent_cancel import unregister_chat_session
    from src.web.crawl_agent_chat_service import get_crawl_agent_chat_service
    from src.web.crawl_agent_run_registry import finish_run, publish_event, set_hermes_run_id

    chat_svc = get_crawl_agent_chat_service()
    collected_blocks: list[dict[str, Any]] = []
    final_content = ""
    run_status = "completed"
    flush_every = 0

    try:
        async for event in chat_svc.stream_chat(
            message,
            history=history,
            session_id=session_id,
            cancel_event=cancel_event,
            agent_profile=agent_profile,
        ):
            ev_dict = event.to_sse_dict()
            run_id = (event.data or {}).get("run_id") if event.type == "thinking" else None
            if run_id:
                set_hermes_run_id(session_id, str(run_id))
                try:
                    set_active_hermes_run_id(session_id, str(run_id), agent_profile=agent_profile)
                except (LookupError, ValueError) as exc:
                    logger.debug("persist hermes_run_id failed session=%s: %s", session_id, exc)

            if event.type in (
                "thinking",
                "plan",
                "tool",
                "result",
                "error",
                "url_crawl",
                "cancelled",
                "message_delta",
                "ui",
            ):
                collected_blocks.append(ev_dict)
                publish_event(session_id, ev_dict)
                if event.type == "cancelled":
                    run_status = "cancelled"
                flush_every += 1
                should_flush = event.type in (
                    "thinking",
                    "plan",
                    "tool",
                    "result",
                    "error",
                    "cancelled",
                    "ui",
                ) or (
                    event.type == "message_delta" and flush_every % 8 == 0
                ) or (event.type == "url_crawl" and flush_every % 3 == 0)
                if should_flush:
                    try:
                        update_streaming_turn(
                            session_id,
                            blocks=collected_blocks,
                            assistant_content=final_content,
                            agent_profile=agent_profile,
                        )
                    except (LookupError, ValueError) as exc:
                        logger.debug("partial flush failed session=%s: %s", session_id, exc)
            elif event.type == "message":
                final_content = event.content or ""
                publish_event(session_id, ev_dict)
                try:
                    update_streaming_turn(
                        session_id,
                        blocks=collected_blocks,
                        assistant_content=final_content,
                        agent_profile=agent_profile,
                    )
                except (LookupError, ValueError) as exc:
                    logger.debug("partial flush failed session=%s: %s", session_id, exc)
            elif event.type == "done":
                publish_event(session_id, ev_dict)
    finally:
        unregister_chat_session(session_id)
        try:
            finalize_streaming_turn(
                session_id,
                blocks=collected_blocks,
                assistant_content=final_content,
                agent_profile=agent_profile,
            )
        except (LookupError, ValueError) as exc:
            logger.warning("持久化对话失败 session=%s: %s", session_id, exc)
        finish_run(session_id, status=run_status)


async def _resume_hermes_background(
    *,
    session_id: str,
    hermes_run_id: str,
    agent_profile: str,
    cancel_event: threading.Event,
) -> None:
    from src.core.crawl_agent_chat_store import (
        finalize_streaming_turn,
        get_session,
        update_streaming_turn,
    )
    from src.core.hermes_agent_chat_client import get_hermes_agent_chat_client
    from src.web.crawl_agent_cancel import is_chat_cancelled, register_chat_session, unregister_chat_session
    from src.web.crawl_agent_chat_service import map_hermes_run_event
    from src.web.crawl_agent_run_registry import finish_run, get_run_info, publish_event, set_hermes_run_id, start_run

    session = get_session(session_id, agent_profile=agent_profile)
    if session is None:
        return

    user_message = _last_user_message(session) or ""
    if not start_run(session_id, user_message):
        info = get_run_info(session_id)
        if info is None or info.get("status") != "running":
            return

    set_hermes_run_id(session_id, hermes_run_id)
    register_chat_session(session_id)

    collected_blocks = _assistant_blocks(session)
    final_content = _assistant_content(session)
    run_status = "completed"
    flush_every = len(collected_blocks)
    tool_turns = sum(1 for b in collected_blocks if b.get("type") == "result")
    client = get_hermes_agent_chat_client()
    terminal_types = {"run.completed", "run.failed", "run.cancelled"}
    saw_terminal = False

    def _append_event(ev_dict: dict[str, Any], chat_type: str) -> None:
        nonlocal final_content, run_status, flush_every
        if collected_blocks and collected_blocks[-1] == ev_dict:
            return
        if chat_type in (
            "thinking",
            "plan",
            "tool",
            "result",
            "error",
            "url_crawl",
            "cancelled",
            "message_delta",
            "ui",
        ):
            collected_blocks.append(ev_dict)
            publish_event(session_id, ev_dict)
            if chat_type == "cancelled":
                run_status = "cancelled"
            flush_every += 1
            should_flush = chat_type in (
                "thinking",
                "plan",
                "tool",
                "result",
                "error",
                "cancelled",
                "ui",
            ) or (
                chat_type == "message_delta" and flush_every % 8 == 0
            ) or (chat_type == "url_crawl" and flush_every % 3 == 0)
            if should_flush:
                try:
                    update_streaming_turn(
                        session_id,
                        blocks=collected_blocks,
                        assistant_content=final_content,
                        agent_profile=agent_profile,
                    )
                except (LookupError, ValueError) as exc:
                    logger.debug("resume partial flush failed session=%s: %s", session_id, exc)
        elif chat_type == "message":
            final_content = ev_dict.get("content") or ""
            publish_event(session_id, ev_dict)
            try:
                update_streaming_turn(
                    session_id,
                    blocks=collected_blocks,
                    assistant_content=final_content,
                    agent_profile=agent_profile,
                )
            except (LookupError, ValueError) as exc:
                logger.debug("resume partial flush failed session=%s: %s", session_id, exc)
        elif chat_type == "done":
            publish_event(session_id, ev_dict)

    try:
        async for raw in client.stream_run_events(
            hermes_run_id,
            cancel_event=cancel_event,
            is_cancelled=lambda: is_chat_cancelled(session_id),
        ):
            if cancel_event.is_set() or is_chat_cancelled(session_id):
                run_status = "cancelled"
                break

            ev_type = raw.get("event") or ""
            if ev_type == "tool.completed":
                tool_turns += 1
            if ev_type == "run.completed":
                raw = {**raw, "_tool_turns": tool_turns}

            for chat_ev in map_hermes_run_event(raw):
                ev_dict = chat_ev.to_sse_dict()
                _append_event(ev_dict, chat_ev.type)

            if ev_type in terminal_types:
                saw_terminal = True
                if ev_type == "run.failed":
                    run_status = "completed"
                elif ev_type == "run.cancelled":
                    run_status = "cancelled"
                break
    finally:
        unregister_chat_session(session_id)
        if not saw_terminal and run_status == "completed":
            run_status = "completed"
        try:
            finalize_streaming_turn(
                session_id,
                blocks=collected_blocks,
                assistant_content=final_content,
                agent_profile=agent_profile,
            )
        except (LookupError, ValueError) as exc:
            logger.warning("resume 持久化失败 session=%s: %s", session_id, exc)
        finish_run(session_id, status=run_status)


def _last_user_message(session: dict[str, Any]) -> Optional[str]:
    for msg in reversed(session.get("messages") or []):
        if msg.get("role") == "user":
            text = (msg.get("content") or "").strip()
            if text:
                return text
    return None


def _assistant_blocks(session: dict[str, Any]) -> list[dict[str, Any]]:
    messages = session.get("messages") or []
    if not messages:
        return []
    last = messages[-1]
    if last.get("role") != "assistant":
        return []
    return list(last.get("blocks") or [])


def _assistant_content(session: dict[str, Any]) -> str:
    messages = session.get("messages") or []
    if not messages:
        return ""
    last = messages[-1]
    if last.get("role") != "assistant":
        return ""
    return (last.get("content") or "").strip()
