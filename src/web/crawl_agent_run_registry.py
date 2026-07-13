"""Hermes Crawl Agent 进行中 run 的内存注册表 — 事件缓冲 + SSE 重连订阅。"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

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
    }
)


@dataclass
class _RunState:
    session_id: str
    user_message: str
    status: str = "running"
    hermes_run_id: Optional[str] = None
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    done: asyncio.Event = field(default_factory=asyncio.Event)


_lock = threading.Lock()
_runs: dict[str, _RunState] = {}


def start_run(session_id: str, user_message: str, *, seed_events: Optional[list[dict[str, Any]]] = None) -> bool:
    """注册新 run；若同 session 已有 running run 则返回 False。"""
    with _lock:
        existing = _runs.get(session_id)
        if existing is not None and existing.status == "running":
            return False
        state = _RunState(session_id=session_id, user_message=user_message)
        state.done = asyncio.Event()
        if seed_events:
            state.events = list(seed_events)
        _runs[session_id] = state
        return True


def publish_event(session_id: str, event: dict[str, Any]) -> int:
    """缓冲可回放事件并通知订阅者。返回 1-based 序号；ping 等非缓冲类型返回 0。"""
    if event.get("type") not in _BUFFER_TYPES:
        return 0
    with _lock:
        state = _runs.get(session_id)
        if state is None:
            return 0
        state.events.append(event)
        seq = len(state.events)
        for queue in list(state.subscribers):
            try:
                queue.put_nowait((seq, event))
            except asyncio.QueueFull:
                pass
        return seq


def finish_run(session_id: str, *, status: str = "completed") -> None:
    with _lock:
        state = _runs.get(session_id)
        if state is None:
            return
        state.status = status
        state.done.set()
        for queue in list(state.subscribers):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass


def clear_run(session_id: str) -> None:
    with _lock:
        _runs.pop(session_id, None)


def set_hermes_run_id(session_id: str, hermes_run_id: str) -> None:
    with _lock:
        state = _runs.get(session_id)
        if state is not None:
            state.hermes_run_id = hermes_run_id


def get_run_info(session_id: str) -> Optional[dict[str, Any]]:
    with _lock:
        state = _runs.get(session_id)
        if state is None:
            return None
        info: dict[str, Any] = {
            "status": state.status,
            "user_message": state.user_message,
            "event_count": len(state.events),
        }
        if state.hermes_run_id:
            info["hermes_run_id"] = state.hermes_run_id
        return info


def is_run_active(session_id: str) -> bool:
    with _lock:
        state = _runs.get(session_id)
        return state is not None and state.status == "running"


async def stream_run_events(session_id: str, *, after: int = 0) -> AsyncIterator[dict[str, Any]]:
    """从 after 起回放已缓冲事件，run 仍进行中时继续推送增量。"""
    with _lock:
        state = _runs.get(session_id)
        if state is None:
            return
        pending = state.events[after:]
        if state.status != "running":
            for event in pending:
                yield event
            return
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        state.subscribers.append(queue)

    try:
        for event in pending:
            yield event
        # 已回放 pending 后，仅接受序号更大的增量事件，避免与 pending 重复
        yield_cursor = after + len(pending)
        while True:
            item = await queue.get()
            if item is None:
                break
            seq, event = item
            if seq <= yield_cursor:
                continue
            yield_cursor = seq
            yield event
    finally:
        with _lock:
            if state is not None and queue in state.subscribers:
                state.subscribers.remove(queue)
