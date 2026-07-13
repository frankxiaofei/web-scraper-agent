"""Redis / 内存 去重层。"""

from __future__ import annotations

import logging
from typing import Optional, Protocol, Set

logger = logging.getLogger(__name__)

URL_SET_KEY = "bid:urls"
HASH_SET_KEY = "bid:content_hashes"


class DedupStore(Protocol):
    def is_duplicate(self, url: str, content_hash: str) -> bool: ...
    def mark_seen(self, url: str, content_hash: str) -> None: ...


class MemoryDedupStore:
    """内存 + JSONL 持久化去重（Pipeline 主存储）。"""

    def __init__(self) -> None:
        self._seen_urls: Set[str] = set()
        self._seen_hashes: Set[str] = set()

    @property
    def seen_urls(self) -> Set[str]:
        return self._seen_urls

    @property
    def seen_hashes(self) -> Set[str]:
        return self._seen_hashes

    def is_duplicate(self, url: str, content_hash: str) -> bool:
        return url in self._seen_urls or content_hash in self._seen_hashes

    def mark_seen(self, url: str, content_hash: str) -> None:
        self._seen_urls.add(url)
        self._seen_hashes.add(content_hash)


class RedisDedupStore:
    """Redis SET 去重，失败时降级到内存。"""

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int = 0,
        fallback: Optional[MemoryDedupStore] = None,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._fallback = fallback or MemoryDedupStore()
        self._client = None
        self._available = False
        try:
            import redis

            self._client = redis.from_url(
                redis_url,
                decode_responses=True,
                socket_timeout=3,
                socket_connect_timeout=3,
            )
            self._client.ping()
            self._available = True
            logger.info("Redis 去重已启用: %s", redis_url.split("@")[-1])
        except Exception as e:
            logger.warning("Redis 不可用，使用内存+JSONL 去重: %s", e)

    @property
    def available(self) -> bool:
        return self._available

    def _refresh_ttl(self) -> None:
        if self._client is None or self._ttl_seconds <= 0:
            return
        try:
            self._client.expire(URL_SET_KEY, self._ttl_seconds)
            self._client.expire(HASH_SET_KEY, self._ttl_seconds)
        except Exception as e:
            logger.warning("Redis TTL 刷新失败: %s", e)

    def is_duplicate(self, url: str, content_hash: str) -> bool:
        if self._fallback.is_duplicate(url, content_hash):
            return True
        if not self._available or self._client is None:
            return False
        try:
            if self._client.sismember(URL_SET_KEY, url):
                return True
            if self._client.sismember(HASH_SET_KEY, content_hash):
                return True
        except Exception as e:
            logger.warning("Redis 去重检查失败: %s", e)
        return False

    def mark_seen(self, url: str, content_hash: str) -> None:
        self._fallback.mark_seen(url, content_hash)
        if not self._available or self._client is None:
            return
        try:
            self._client.sadd(URL_SET_KEY, url)
            self._client.sadd(HASH_SET_KEY, content_hash)
            self._refresh_ttl()
        except Exception as e:
            logger.warning("Redis 写入失败: %s", e)


def create_dedup_store(
    redis_url: Optional[str] = None,
    ttl_seconds: int = 0,
    memory: Optional[MemoryDedupStore] = None,
) -> MemoryDedupStore | RedisDedupStore:
    memory = memory or MemoryDedupStore()
    if redis_url:
        return RedisDedupStore(redis_url, ttl_seconds=ttl_seconds, fallback=memory)
    return memory
