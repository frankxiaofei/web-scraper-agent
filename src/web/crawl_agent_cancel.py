"""Hermes Crawl Agent 对话流中止 — per-session cancel flag + skill session 清理。"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

_thread_session = threading.local()


class CrawlAgentCancelledError(Exception):
    """用户请求中止 Crawl Agent 对话爬取。"""


@dataclass
class _ActiveChat:
    cancel_event: threading.Event = field(default_factory=threading.Event)
    skill_keys: set[str] = field(default_factory=set)


_lock = threading.Lock()
_active: dict[str, _ActiveChat] = {}


def _skill_key(site_id: str, session_id: Optional[str]) -> str:
    return f"{site_id}:{session_id or 'default'}"


def register_chat_session(session_id: str) -> threading.Event:
    """注册进行中的对话 session，返回 cancel Event。"""
    ev = threading.Event()
    with _lock:
        _active[session_id] = _ActiveChat(cancel_event=ev)
    return ev


def unregister_chat_session(session_id: str) -> None:
    with _lock:
        _active.pop(session_id, None)


def get_cancel_event(session_id: str) -> Optional[threading.Event]:
    with _lock:
        state = _active.get(session_id)
        return state.cancel_event if state else None


def is_chat_cancelled(session_id: Optional[str] = None) -> bool:
    sid = session_id or getattr(_thread_session, "session_id", None)
    if not sid:
        return False
    with _lock:
        state = _active.get(sid)
        return state is not None and state.cancel_event.is_set()


def set_thread_chat_session(session_id: Optional[str]) -> None:
    _thread_session.session_id = session_id


def track_skill_session(
    site_id: str,
    skill_session_id: Optional[str] = None,
    *,
    chat_session_id: Optional[str] = None,
) -> None:
    sid = chat_session_id or getattr(_thread_session, "session_id", None)
    if not sid:
        return
    key = _skill_key(site_id, skill_session_id)
    with _lock:
        state = _active.get(sid)
        if state is not None:
            state.skill_keys.add(key)


def _close_skill_sessions(keys: set[str]) -> None:
    if not keys:
        return
    from src.web.crawl_agent_skills import close_skill_session, run_async

    for key in list(keys):
        if ":" not in key:
            continue
        site_id, sess_part = key.split(":", 1)
        skill_sid = None if sess_part == "default" else sess_part
        try:
            run_async(close_skill_session(site_id, skill_sid), timeout=30)
        except Exception:
            logger.debug("关闭 skill session 失败 key=%s", key, exc_info=True)


def cancel_chat_session(session_id: str) -> dict[str, object]:
    """设置 cancel flag 并关闭关联 browser skill sessions。"""
    with _lock:
        state = _active.get(session_id)
        if state is None:
            return {"ok": False, "session_id": session_id, "error": "无进行中的对话流"}
        state.cancel_event.set()
        skill_keys = set(state.skill_keys)

    _close_skill_sessions(skill_keys)
    return {"ok": True, "session_id": session_id, "cancelled": True}


def raise_if_chat_cancelled(session_id: Optional[str] = None) -> None:
    if is_chat_cancelled(session_id):
        raise CrawlAgentCancelledError("用户已中止爬取")
