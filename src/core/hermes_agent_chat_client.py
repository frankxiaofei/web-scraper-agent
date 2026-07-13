"""Hermes Agent API Server 客户端 — /hermes 对话经 POST /v1/runs + SSE events 驱动。"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, AsyncIterator, Callable, Optional
from urllib.parse import urlparse, urlunparse

import httpx

from src.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

RUNS_PATH = "/v1/runs"


def resolve_hermes_agent_chat_url(settings: Optional[Settings] = None) -> str:
    """解析 Hermes 对话 API 基址（8642 API Server，区别于 8080 crawl dispatch）。"""
    cfg = settings or get_settings()
    explicit = (cfg.hermes_agent_chat_url or "").strip()
    if explicit:
        return explicit.rstrip("/")

    base = (cfg.hermes_agent_url or "").strip()
    if not base:
        return ""

    base = base.rstrip("/")
    if ":8080" in base:
        return base.replace(":8080", ":8642", 1)

    parsed = urlparse(base)
    if parsed.port is None and parsed.scheme in ("http", "https"):
        host = parsed.hostname or ""
        port = 443 if parsed.scheme == "https" else 8642
        netloc = f"{host}:{port}"
        if parsed.username:
            auth = parsed.username
            if parsed.password:
                auth = f"{auth}:{parsed.password}"
            netloc = f"{auth}@{netloc}"
        return urlunparse((parsed.scheme, netloc, parsed.path, "", "", "")).rstrip("/")

    return base


def _auth_headers(settings: Optional[Settings] = None) -> dict[str, str]:
    cfg = settings or get_settings()
    headers: dict[str, str] = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    key = (cfg.hermes_agent_api_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


class HermesAgentChatClient:
    """web_scraper /hermes 对话 → hermes-agent API Server（/v1/runs + SSE）。"""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self.base_url = (base_url or resolve_hermes_agent_chat_url(self._settings)).rstrip("/")
        self.timeout = float(
            timeout if timeout is not None else self._settings.hermes_agent_timeout
        )

    @property
    def available(self) -> bool:
        return bool(self.base_url)

    async def health_check(self) -> bool:
        if not self.base_url:
            return False
        url = f"{self.base_url}/health"
        try:
            async with httpx.AsyncClient(timeout=min(10.0, self.timeout)) as client:
                resp = await client.get(url, headers=_auth_headers())
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def start_run(
        self,
        *,
        user_message: str,
        conversation_history: Optional[list[dict[str, str]]] = None,
        session_id: Optional[str] = None,
        instructions: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        """POST /v1/runs，返回 (run_id, error)。"""
        if not self.base_url:
            return None, "HERMES_AGENT_CHAT_URL / HERMES_AGENT_URL 未配置"

        body: dict[str, Any] = {"input": user_message}
        if conversation_history:
            body["conversation_history"] = conversation_history
        # 不传 session_id：避免 Hermes 持久 session 内 tool 失败历史导致 LLM 只回文字不调工具。
        # 跨轮上下文由 conversation_history（web_scraper 会话存储）提供。
        # 注：hermes-agent POST /v1/runs 不接受 per-run max_iterations；预算由
        # HERMES_MAX_ITERATIONS 或 data/hermes-agent/config.yaml agent.max_turns 控制。
        if instructions:
            body["instructions"] = instructions

        url = f"{self.base_url}{RUNS_PATH}"
        headers = _auth_headers()

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=body, headers=headers)
            if resp.status_code >= 400:
                detail = resp.text[:500]
                try:
                    payload = resp.json()
                    detail = (
                        payload.get("error", {}).get("message")
                        or payload.get("error")
                        or detail
                    )
                except json.JSONDecodeError:
                    pass
                return None, f"Hermes Agent 启动失败 (HTTP {resp.status_code}): {detail}"
            payload = resp.json()
            run_id = payload.get("run_id")
            if not run_id:
                return None, "Hermes Agent 未返回 run_id"
            return str(run_id), None
        except httpx.TimeoutException as exc:
            return None, f"Hermes Agent 请求超时: {exc}"
        except httpx.HTTPError as exc:
            return None, f"Hermes Agent 连接失败: {exc}"

    async def stop_run(self, run_id: str) -> None:
        if not self.base_url or not run_id:
            return
        url = f"{self.base_url}{RUNS_PATH}/{run_id}/stop"
        try:
            async with httpx.AsyncClient(timeout=min(30.0, self.timeout)) as client:
                await client.post(url, headers=_auth_headers())
        except httpx.HTTPError:
            logger.debug("Hermes stop_run failed run_id=%s", run_id, exc_info=True)

    async def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        """GET /v1/runs/{run_id} — 查询 run 状态。"""
        if not self.base_url or not run_id:
            return None
        url = f"{self.base_url}{RUNS_PATH}/{run_id}"
        try:
            async with httpx.AsyncClient(timeout=min(15.0, self.timeout)) as client:
                resp = await client.get(url, headers=_auth_headers())
            if resp.status_code >= 400:
                return None
            payload = resp.json()
            return payload if isinstance(payload, dict) else None
        except httpx.HTTPError:
            logger.debug("Hermes get_run failed run_id=%s", run_id, exc_info=True)
            return None

    def get_run_status_sync(self, run_id: str) -> Optional[str]:
        """同步查询 Hermes run 状态（running/completed/failed 等）。"""
        if not self.base_url or not run_id:
            return None
        url = f"{self.base_url}{RUNS_PATH}/{run_id}"
        try:
            with httpx.Client(timeout=min(15.0, self.timeout)) as client:
                resp = client.get(url, headers=_auth_headers())
            if resp.status_code >= 400:
                return None
            payload = resp.json()
            if not isinstance(payload, dict):
                return None
            status = payload.get("status") or payload.get("state")
            if isinstance(status, str):
                return status.lower()
            return None
        except httpx.HTTPError:
            logger.debug("Hermes get_run_status_sync failed run_id=%s", run_id, exc_info=True)
            return None

    async def stream_run_events(
        self,
        run_id: str,
        *,
        cancel_event: Optional[threading.Event] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """GET /v1/runs/{run_id}/events — 解析 SSE data 行。"""
        if not self.base_url:
            return

        url = f"{self.base_url}{RUNS_PATH}/{run_id}/events"
        headers = _auth_headers()
        headers["Accept"] = "text/event-stream"
        stream_timeout = httpx.Timeout(connect=15.0, read=3600.0, write=30.0, pool=15.0)

        async with httpx.AsyncClient(timeout=stream_timeout) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", errors="replace")[:500]
                    yield {
                        "event": "run.failed",
                        "error": f"SSE 订阅失败 (HTTP {resp.status_code}): {body}",
                    }
                    return

                async for line in resp.aiter_lines():
                    if cancel_event and cancel_event.is_set():
                        await self.stop_run(run_id)
                        yield {"event": "run.cancelled"}
                        return
                    if is_cancelled and is_cancelled():
                        await self.stop_run(run_id)
                        yield {"event": "run.cancelled"}
                        return

                    if not line or not line.startswith("data: "):
                        continue
                    raw = line[6:].strip()
                    if not raw:
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.debug("invalid hermes SSE json: %s", raw[:200])
                        continue
                    if isinstance(event, dict):
                        yield event


_default_client: Optional[HermesAgentChatClient] = None


def get_hermes_agent_chat_client() -> HermesAgentChatClient:
    global _default_client
    if _default_client is None:
        _default_client = HermesAgentChatClient()
    return _default_client


def reset_hermes_agent_chat_client() -> None:
    """测试或 .env 热更新后丢弃单例（下次 get 会重建 base_url/timeout）。"""
    global _default_client
    _default_client = None
