"""Chrome 插件录制 WebSocket 连接管理。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class RecorderWsManager:
    """按 session 维护 workflow 页与 extension 的 WebSocket 连接。"""

    def __init__(self) -> None:
        self._page_sockets: dict[str, WebSocket] = {}
        self._extension_sockets: dict[str, WebSocket] = {}
        self._lock = asyncio.Lock()

    async def register(self, session_id: str, ws: WebSocket, role: str) -> None:
        async with self._lock:
            bucket = self._page_sockets if role == "page" else self._extension_sockets
            old = bucket.get(session_id)
            if old is not None and old is not ws:
                try:
                    await old.close()
                except Exception:
                    pass
            bucket[session_id] = ws

    async def unregister(self, session_id: str, role: str) -> None:
        async with self._lock:
            if role == "page":
                self._page_sockets.pop(session_id, None)
            else:
                self._extension_sockets.pop(session_id, None)

    async def send_to_page(self, session_id: str, message: dict[str, Any]) -> bool:
        ws = self._page_sockets.get(session_id)
        if ws is None:
            return False
        try:
            await ws.send_json(message)
            return True
        except Exception as exc:
            logger.debug("send_to_page failed session=%s: %s", session_id, exc)
            await self.unregister(session_id, "page")
            return False

    async def send_to_extension(self, session_id: str, message: dict[str, Any]) -> bool:
        ws = self._extension_sockets.get(session_id)
        if ws is None:
            return False
        try:
            await ws.send_json(message)
            return True
        except Exception as exc:
            logger.debug("send_to_extension failed session=%s: %s", session_id, exc)
            await self.unregister(session_id, "extension")
            return False

    def has_extension(self, session_id: str) -> bool:
        return session_id in self._extension_sockets

    def has_page(self, session_id: str) -> bool:
        return session_id in self._page_sockets

    def connection_status(self, session_id: str) -> dict[str, bool]:
        return {
            "page_connected": self.has_page(session_id),
            "extension_connected": self.has_extension(session_id),
        }


_ws_manager: Optional[RecorderWsManager] = None


def get_recorder_ws_manager() -> RecorderWsManager:
    global _ws_manager
    if _ws_manager is None:
        _ws_manager = RecorderWsManager()
    return _ws_manager
