"""同步任务取消令牌：通过 contextvar 在 adapter / RuleExecutor 调用链中传播。"""

from __future__ import annotations

import asyncio
import contextvars
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

_cancel_event: contextvars.ContextVar[Optional[asyncio.Event]] = contextvars.ContextVar(
    "sync_cancel_event", default=None
)


class SyncCancelledError(Exception):
    """用户请求中止同步。"""


def check_sync_cancelled() -> bool:
    ev = _cancel_event.get()
    return ev is not None and ev.is_set()


def raise_if_sync_cancelled() -> None:
    if check_sync_cancelled():
        raise SyncCancelledError("用户已中止同步")


@asynccontextmanager
async def sync_cancel_scope(cancel_event: asyncio.Event) -> AsyncIterator[None]:
    token = _cancel_event.set(cancel_event)
    try:
        yield
    finally:
        _cancel_event.reset(token)
