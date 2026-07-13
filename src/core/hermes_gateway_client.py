"""Hermes Agent Gateway HTTP 客户端 — scheduler 经外部 hermes-agent 容器触发 crawl/sync。

调度器在 ``HERMES_AGENT_URL`` 已配置时调用 ``POST /v1/crawl/dispatch``（由
``docker/hermes-agent/init-config/crawl_dispatch_server.py`` 在 hermes-agent 容器内提供），
由 Gateway 侧 crawl toolset 再 HTTP 回调 web_scraper ``/api/sync/*`` 或
``/api/crawl-agent/skills/execute``。

app / hermes 容器执行实际 sync 时不应再走 Gateway（避免 crawl_trigger → Runner 递归）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional
from urllib.parse import quote

import httpx

from src.core.config import get_settings

logger = logging.getLogger(__name__)

DEFAULT_DISPATCH_PATH = "/v1/crawl/dispatch"
DEFAULT_RETRIES = 3
DEFAULT_RETRY_BACKOFF = 2.0


def should_use_hermes_gateway() -> bool:
    """是否由 scheduler 将 crawl 任务委托给外部 hermes-agent Gateway。"""
    settings = get_settings()
    if not settings.crawl_via_hermes:
        return False
    if not (settings.hermes_agent_url or "").strip():
        return False
    role = os.getenv("WEB_SCRAPER_ROLE", "").strip().lower()
    if role == "scheduler":
        return True
    explicit = os.getenv("HERMES_GATEWAY_CLIENT", "").strip().lower()
    return explicit in ("1", "true", "yes")


def should_execute_sync_directly() -> bool:
    """当前进程是否应在本机直接执行 sync_site（不经 Gateway 委托）。"""
    settings = get_settings()
    if not settings.crawl_via_hermes:
        return True
    return not should_use_hermes_gateway()


def _gateway_base_url() -> str:
    return (get_settings().hermes_agent_url or "").rstrip("/")


def _auth_headers() -> dict[str, str]:
    settings = get_settings()
    headers: dict[str, str] = {"Accept": "application/json"}
    key = (settings.hermes_agent_api_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _unwrap_dispatch_result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"ok": False, "error": "invalid gateway response", "raw": payload}
    if payload.get("ok") is False:
        return payload
    result = payload.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return {"ok": True, "raw": result}
    if isinstance(result, dict):
        inner = result.get("data") if result.get("success") else result
        if isinstance(inner, dict):
            merged = {**payload, **inner}
            merged.setdefault("ok", result.get("success", payload.get("ok", True)))
            merged.setdefault("success", merged.get("ok", True))
            return merged
        merged = {**payload, "result": result}
        merged.setdefault("success", result.get("success", payload.get("ok", True)))
        return merged
    return payload


class HermesGatewayClient:
    """hermes-agent Gateway crawl dispatch 客户端（httpx + 重试）。"""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.hermes_agent_url or "").rstrip("/")
        self.timeout = float(timeout if timeout is not None else settings.hermes_agent_timeout)
        self.retries = max(1, int(retries if retries is not None else settings.hermes_agent_retries))

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

    async def dispatch(
        self,
        *,
        tool: str,
        arguments: Optional[dict[str, Any]] = None,
        task_type: Optional[str] = None,
        site_id: Optional[str] = None,
        wait: bool = False,
        poll_timeout_seconds: Optional[int] = None,
    ) -> dict[str, Any]:
        if not self.base_url:
            return {"ok": False, "success": False, "error": "HERMES_AGENT_URL 未配置"}

        body: dict[str, Any] = {
            "tool": tool,
            "arguments": arguments or {},
        }
        if task_type:
            body["task_type"] = task_type
        if site_id:
            body["site_id"] = site_id
        if wait:
            body["wait"] = True
        if poll_timeout_seconds is not None:
            body["poll_timeout_seconds"] = poll_timeout_seconds

        url = f"{self.base_url}{DEFAULT_DISPATCH_PATH}"
        last_error: Optional[str] = None

        for attempt in range(1, self.retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(url, json=body, headers=_auth_headers())
                if resp.status_code >= 500 and attempt < self.retries:
                    last_error = f"HTTP {resp.status_code}"
                    await asyncio.sleep(DEFAULT_RETRY_BACKOFF * attempt)
                    continue
                try:
                    payload = resp.json()
                except json.JSONDecodeError:
                    payload = {
                        "ok": False,
                        "success": False,
                        "error": resp.text[:500] or f"HTTP {resp.status_code}",
                        "http_status": resp.status_code,
                    }
                if resp.status_code >= 400:
                    payload.setdefault("ok", False)
                    payload.setdefault("success", False)
                    if resp.status_code >= 500 and attempt < self.retries:
                        last_error = str(payload.get("error") or resp.status_code)
                        await asyncio.sleep(DEFAULT_RETRY_BACKOFF * attempt)
                        continue
                    return payload
                return _unwrap_dispatch_result(payload)
            except httpx.TimeoutException as exc:
                last_error = f"timeout: {exc}"
                if attempt < self.retries:
                    await asyncio.sleep(DEFAULT_RETRY_BACKOFF * attempt)
                    continue
            except httpx.HTTPError as exc:
                last_error = str(exc)
                if attempt < self.retries:
                    await asyncio.sleep(DEFAULT_RETRY_BACKOFF * attempt)
                    continue

        return {
            "ok": False,
            "success": False,
            "error": last_error or "gateway dispatch failed",
            "gateway_url": self.base_url,
        }

    async def run_site_sync(
        self,
        site_id: str,
        *,
        trigger: str = "scheduled",
        max_items: Optional[int] = None,
        wait: bool = True,
        poll_timeout_seconds: int = 3600,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {"site_id": site_id}
        if max_items is not None:
            arguments["max_items"] = max_items
        outcome = await self.dispatch(
            tool="crawl_trigger",
            arguments=arguments,
            task_type="site_sync",
            site_id=site_id,
            wait=wait,
            poll_timeout_seconds=poll_timeout_seconds if wait else None,
        )
        outcome.setdefault("site_id", site_id)
        outcome.setdefault("trigger", trigger)
        outcome["hermes_gateway"] = True
        outcome["task_type"] = "site_sync"
        if "success" not in outcome:
            outcome["success"] = bool(outcome.get("ok"))
        return outcome

    async def run_daily_bim_sync(
        self,
        *,
        site_id: Optional[str] = None,
        max_items: Optional[int] = None,
        stagger: Optional[int] = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {}
        if site_id:
            arguments["site_id"] = site_id
        if max_items is not None:
            arguments["max_items"] = max_items
        if stagger is not None:
            arguments["stagger"] = stagger
        outcome = await self.dispatch(
            tool="crawl_bim_sync_all",
            arguments=arguments,
            task_type="daily_bim_sync",
            site_id=site_id,
            wait=True,
            poll_timeout_seconds=7200,
        )
        outcome["hermes_gateway"] = True
        outcome["task_type"] = "daily_bim_sync"
        if "success" not in outcome:
            outcome["success"] = bool(outcome.get("ok"))
        return outcome


_default_client: Optional[HermesGatewayClient] = None


def get_hermes_gateway_client() -> HermesGatewayClient:
    global _default_client
    if _default_client is None:
        _default_client = HermesGatewayClient()
    return _default_client


async def gateway_run_site_sync(
    site_id: str,
    *,
    trigger: str = "scheduled",
    max_items: Optional[int] = None,
) -> dict[str, Any]:
    return await get_hermes_gateway_client().run_site_sync(
        site_id,
        trigger=trigger,
        max_items=max_items,
    )


async def gateway_run_daily_bim_sync(
    *,
    site_id: Optional[str] = None,
    max_items: Optional[int] = None,
    stagger: Optional[int] = None,
) -> dict[str, Any]:
    return await get_hermes_gateway_client().run_daily_bim_sync(
        site_id=site_id,
        max_items=max_items,
        stagger=stagger,
    )


def gateway_site_status_path(site_id: str) -> str:
    return f"/api/sync/{quote(site_id, safe='')}/status"
