"""进程内共享 MongoClient，避免多 store 各自 3s 连接超时拖慢 SSR。"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_client: Any = None
_db_name: str = ""
_available: bool = False
_checked_at: float = 0.0
_retry_seconds: float = 30.0
_lock = threading.Lock()


def get_mongo_database(
    uri: Optional[str],
    db_name: str,
    *,
    server_selection_timeout_ms: int = 1000,
) -> Any:
    """返回 Mongo database 句柄；不可用时 30s 内不再重试。"""
    global _client, _db_name, _available, _checked_at

    if not uri:
        return None

    with _lock:
        now = time.monotonic()
        if _available and _client is not None and _db_name == db_name:
            return _client[_db_name]
        if not _available and now - _checked_at < _retry_seconds:
            return None

        _checked_at = now
        _available = False
        _client = None
        _db_name = db_name

        try:
            from pymongo import MongoClient
            from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

            client = MongoClient(uri, serverSelectionTimeoutMS=server_selection_timeout_ms)
            client.admin.command("ping")
            _client = client
            _available = True
            logger.info("MongoDB 已连接: %s", db_name)
            return client[db_name]
        except BaseException as e:
            logger.info("MongoDB 不可用，已降级: %s", e)
            return None


def mongo_is_available() -> bool:
    return _available
