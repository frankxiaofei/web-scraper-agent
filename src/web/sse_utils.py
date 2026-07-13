"""SSE streaming helpers — keepalive for long-running crawl agent chats."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, AsyncIterator, Callable, TypeVar

T = TypeVar("T")

SSE_KEEPALIVE_INTERVAL = 15.0  # 代理 idle 断开前的心跳间隔（秒）；hermes-agent SSE 侧另有 30s keepalive
SSE_PING_COMMENT = ": ping\n\n"
SSE_PING_DATA = 'data: {"type":"ping"}\n\n'

# 单次对话流、单次 skill 工具调用的上限（秒）
SSE_CHAT_STREAM_TIMEOUT = 1800.0
SKILL_TOOL_TIMEOUT = 600.0

SSE_STREAM_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def format_sse_data(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def iter_with_idle_keepalive(
    source: AsyncIterator[T],
    *,
    interval: float | None = None,
    ping: str = SSE_PING_COMMENT,
) -> AsyncIterator[T | str]:
    """在 source 空闲超过 interval 时发送 SSE comment ping，避免代理/浏览器 idle 断开。"""
    wait = interval if interval is not None else SSE_KEEPALIVE_INTERVAL
    it = source.__aiter__()
    while True:
        getter = asyncio.create_task(it.__anext__())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(asyncio.shield(getter), timeout=wait)
                    yield item
                    break
                except asyncio.TimeoutError:
                    yield ping
        except StopAsyncIteration:
            break
        finally:
            if not getter.done():
                getter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await getter


async def yield_pings_while_waiting(
    task: asyncio.Task[Any],
    *,
    interval: float | None = None,
    ping_factory: Callable[[], Any] | None = None,
) -> AsyncIterator[Any]:
    """在 task 完成前按 interval 产出 ping 事件（供 stream_chat yield）。"""
    wait = interval if interval is not None else SSE_KEEPALIVE_INTERVAL
    if ping_factory is None:
        ping_factory = lambda: {"type": "ping"}  # noqa: E731
    while not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=wait)
            break
        except asyncio.TimeoutError:
            yield ping_factory()
    if task.cancelled():
        raise asyncio.CancelledError()
    exc = task.exception()
    if exc is not None:
        raise exc


async def to_thread_with_timeout(
    func: Callable[..., T],
    /,
    *args: Any,
    timeout: float = SKILL_TOOL_TIMEOUT,
) -> T:
    """在线程池执行同步函数，带全局超时防挂死。"""
    return await asyncio.wait_for(asyncio.to_thread(func, *args), timeout=timeout)
