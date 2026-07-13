"""纯 HTTP 抓取 crawl_rules API 列表/详情（零 browser）。"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from src.core.config import get_settings
from src.core.crawl_rules import ListPageApi
from src.core.site_credentials import get_current_browser_auth

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=15.0)
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def resolve_http_proxy() -> Optional[str]:
    """从 Settings / 环境变量解析出站 HTTP(S) 代理。"""
    settings = get_settings()
    proxy = (settings.https_proxy or settings.http_proxy or "").strip()
    if proxy:
        return proxy
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return None


def _proxy_log_label(proxy: str) -> str:
    """日志中隐藏代理凭据。"""
    if "@" in proxy:
        return proxy.split("@", 1)[-1]
    return proxy


def create_http_client() -> httpx.AsyncClient:
    """构建带可选代理的 httpx AsyncClient（调用方负责 async with）。"""
    proxy = resolve_http_proxy()
    kwargs: dict[str, Any] = {"timeout": DEFAULT_TIMEOUT, "follow_redirects": True}
    if proxy:
        kwargs["proxy"] = proxy
        logger.debug("HTTP API 使用代理: %s", _proxy_log_label(proxy))
    return httpx.AsyncClient(**kwargs)


def format_http_api_error(exc: Exception) -> str:
    """为 Agent/运维脚本附加 IP 封/代理/VPN 指引。"""
    text = str(exc).lower()
    hints: list[str] = []
    if any(code in text for code in ("500", "502", "503", "504")):
        hints.append(
            "服务器 5xx 或 IP 封锁：在 .env 配置 HTTP_PROXY/HTTPS_PROXY、启用 VPN，"
            "或在可访问该站的内网/云主机运行 scripts/crawl_powerchina_http.py"
        )
    if "403" in text or "401" in text or "forbidden" in text:
        hints.append("鉴权失败：在 Web UI 配置站点凭据 Cookie/headers")
    if "connect" in text or "timeout" in text or "network" in text:
        hints.append("网络不可达：检查代理/VPN 或换可访问 bid.powerchina.cn 的网络")
    if hints:
        return f"{exc}。建议：{'；'.join(hints)}"
    return str(exc)


def _resolve_api_url(api_url: str, *, site_url: str = "", entry_url: str = "") -> str:
    if api_url.startswith("http://") or api_url.startswith("https://"):
        return api_url
    base = entry_url or site_url or "https://example.com"
    from urllib.parse import urljoin

    return urljoin(base if base.endswith("/") else f"{base}/", api_url.lstrip("/"))


def build_http_headers(
    *,
    site_url: str = "",
    entry_url: str = "",
    content_type: Optional[str] = None,
    api_headers: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """合并默认浏览器头、站点凭据 extra_http_headers 与 crawl_rules api.headers。"""
    origin_base = entry_url or site_url or ""
    parsed = urlparse(origin_base)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""

    headers: dict[str, str] = {
        "User-Agent": DEFAULT_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if origin:
        headers["Origin"] = origin
        referer = entry_url or origin_base or origin
        headers["Referer"] = referer
    if content_type:
        headers["Content-Type"] = content_type

    auth = get_current_browser_auth()
    extra = auth.get("extra_http_headers") or {}
    for key, value in extra.items():
        if value:
            headers[str(key)] = str(value)

    if api_headers:
        for key, value in api_headers.items():
            if value is not None and str(value).strip():
                headers[str(key)] = str(value)
    return headers


def _cookie_header_from_auth() -> Optional[str]:
    auth = get_current_browser_auth()
    extra = auth.get("extra_http_headers") or {}
    if extra.get("Cookie"):
        return str(extra["Cookie"])
    cookies = auth.get("cookies") or []
    if not cookies:
        return None
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name"))


def _cookies_from_header(cookie_header: Optional[str]) -> dict[str, str] | None:
    if not cookie_header:
        return None
    cookies: dict[str, str] = {}
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" in part:
            name, value = part.split("=", 1)
            cookies[name.strip()] = value.strip()
    return cookies or None


async def fetch_api_json(
    api: ListPageApi,
    *,
    page_param: str,
    page_num: int,
    site_url: str = "",
    entry_url: str = "",
) -> Any:
    """POST/GET 列表 API，返回解析后的 JSON body。"""
    params = {**api.params, page_param: page_num}
    if api.method == "POST" and "time" not in params:
        params["time"] = int(time.time() * 1000)

    api_url = _resolve_api_url(api.url, site_url=site_url, entry_url=entry_url)
    content_type = "application/json;charset=utf-8" if api.body_format == "json" else None
    headers = build_http_headers(
        site_url=site_url,
        entry_url=entry_url,
        content_type=content_type,
        api_headers=api.headers or None,
    )

    cookie_header = _cookie_header_from_auth()
    cookies = _cookies_from_header(cookie_header if cookie_header and "Cookie" not in headers else None)

    async with create_http_client() as client:
        if api.method == "GET":
            response = await client.get(api_url, params=params, headers=headers, cookies=cookies)
        elif api.body_format == "json":
            response = await client.post(
                api_url,
                content=json.dumps(params, ensure_ascii=False),
                headers=headers,
                cookies=cookies,
            )
        else:
            response = await client.post(api_url, data=params, headers=headers, cookies=cookies)

    if response.status_code >= 400:
        logger.warning(
            "HTTP API 失败 %s status=%s body=%s",
            api_url,
            response.status_code,
            response.text[:500],
        )
        response.raise_for_status()

    try:
        return response.json()
    except json.JSONDecodeError as exc:
        logger.warning("HTTP API 非 JSON %s: %s", api_url, exc)
        raise


async def fetch_json_url(
    url: str,
    *,
    site_url: str = "",
    entry_url: str = "",
    method: str = "GET",
    api_headers: Optional[dict[str, str]] = None,
) -> Any:
    """GET 详情等单 URL JSON API。"""
    headers = build_http_headers(
        site_url=site_url,
        entry_url=entry_url,
        api_headers=api_headers,
    )
    cookie_header = _cookie_header_from_auth()
    cookies = _cookies_from_header(cookie_header)

    async with create_http_client() as client:
        if method.upper() == "POST":
            response = await client.post(url, headers=headers, cookies=cookies)
        else:
            response = await client.get(url, headers=headers, cookies=cookies)

    if response.status_code >= 400:
        logger.warning("HTTP GET 失败 %s status=%s", url, response.status_code)
        response.raise_for_status()
    return response.json()
