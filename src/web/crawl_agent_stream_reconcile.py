"""Hermes 对话 streaming 会话 reconcile — 孤儿 streaming 清理与 Hermes run 恢复。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_HERMES_ACTIVE = frozenset({"running", "pending", "active"})
_HERMES_TERMINAL = frozenset(
    {"completed", "succeeded", "success", "failed", "error", "cancelled", "canceled", "stopped"}
)
_BUFFER_TYPES = frozenset(
    {
        "thinking",
        "plan",
        "tool",
        "result",
        "error",
        "url_crawl",
        "cancelled",
        "message",
        "message_delta",
        "done",
        "ui",
    }
)


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def streaming_age_minutes(session: dict[str, Any]) -> Optional[float]:
    updated = _parse_iso(session.get("updated_at"))
    if updated is None:
        return None
    now = datetime.now(timezone.utc)
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (now - updated).total_seconds() / 60.0


def count_buffered_events(session: dict[str, Any]) -> int:
    """与 run_registry / 前端 BUFFERED_EVENT_TYPES 对齐的可回放事件数。"""
    return sum(
        1
        for block in _assistant_blocks(session)
        if isinstance(block, dict) and block.get("type") in _BUFFER_TYPES
    )


def _spawn_hermes_relay(
    session: dict[str, Any],
    *,
    session_id: str,
    hermes_run_id: str,
    agent_profile: str,
) -> bool:
    from src.web.crawl_agent_run_executor import spawn_hermes_resume
    from src.web.crawl_agent_run_registry import start_run

    seed = _assistant_blocks(session)
    if not start_run(session_id, _last_user_message(session) or "", seed_events=seed):
        return False
    from src.web.crawl_agent_cancel import register_chat_session

    spawn_hermes_resume(
        session_id=session_id,
        hermes_run_id=hermes_run_id,
        agent_profile=agent_profile,
        cancel_event=register_chat_session(session_id),
    )
    return True


def _finalize_orphan_streaming(
    session_id: str,
    *,
    agent_profile: str,
    age: Optional[float],
) -> dict[str, Any]:
    from src.core.crawl_agent_chat_store import finalize_interrupted_turn, get_session

    try:
        finalize_interrupted_turn(session_id, agent_profile=agent_profile)
        logger.info("已 finalize 孤儿 streaming session=%s age=%.1fm", session_id, age if age is not None else -1)
    except (LookupError, ValueError) as exc:
        logger.debug("finalize interrupted failed session=%s: %s", session_id, exc)

    refreshed = get_session(session_id, agent_profile=agent_profile)
    return refreshed if refreshed is not None else {}


def reconcile_streaming_session(
    session: dict[str, Any],
    *,
    agent_profile: str = "default",
    stale_minutes: int = 30,
) -> dict[str, Any]:
    """GET session 时修复孤儿 streaming 或尝试从 Hermes 恢复。"""
    if not session.get("streaming"):
        return session

    session_id = session["session_id"]
    from src.web.crawl_agent_run_executor import is_background_running
    from src.web.crawl_agent_run_registry import is_run_active

    if is_run_active(session_id) or is_background_running(session_id):
        return session

    age = streaming_age_minutes(session)
    hermes_run_id = (session.get("active_hermes_run_id") or "").strip()

    if not hermes_run_id:
        refreshed = _finalize_orphan_streaming(session_id, agent_profile=agent_profile, age=age)
        return refreshed or session

    from src.core.hermes_agent_chat_client import get_hermes_agent_chat_client

    client = get_hermes_agent_chat_client()
    if not client.available:
        if age is not None and age < stale_minutes:
            return session
        refreshed = _finalize_orphan_streaming(session_id, agent_profile=agent_profile, age=age)
        return refreshed or session

    status = client.get_run_status_sync(hermes_run_id)
    if status in _HERMES_ACTIVE:
        if _spawn_hermes_relay(
            session,
            session_id=session_id,
            hermes_run_id=hermes_run_id,
            agent_profile=agent_profile,
        ):
            logger.info("已从 Hermes 恢复 streaming session=%s run=%s", session_id, hermes_run_id)
        return session

    if status in _HERMES_TERMINAL:
        if _spawn_hermes_relay(
            session,
            session_id=session_id,
            hermes_run_id=hermes_run_id,
            agent_profile=agent_profile,
        ):
            logger.info(
                "已从 Hermes 收尾 streaming session=%s run=%s status=%s",
                session_id,
                hermes_run_id,
                status,
            )
        return session

    if status is None and age is not None and age < stale_minutes:
        return session

    refreshed = _finalize_orphan_streaming(session_id, agent_profile=agent_profile, age=age)
    return refreshed or session


def _last_user_message(session: dict[str, Any]) -> str:
    for msg in reversed(session.get("messages") or []):
        if msg.get("role") == "user":
            return (msg.get("content") or "").strip()
    return ""


def _assistant_blocks(session: dict[str, Any]) -> list[dict[str, Any]]:
    messages = session.get("messages") or []
    if not messages:
        return []
    last = messages[-1]
    if last.get("role") != "assistant":
        return []
    return list(last.get("blocks") or [])
