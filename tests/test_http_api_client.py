"""http_api_client 单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.crawl_rules import ListPageApi
from src.core.http_api_client import (
    build_http_headers,
    format_http_api_error,
    resolve_http_proxy,
)


def test_build_http_headers_merges_api_headers():
    headers = build_http_headers(
        site_url="https://bid.powerchina.cn",
        entry_url="https://bid.powerchina.cn/consult/notice",
        content_type="application/json;charset=utf-8",
        api_headers={"Referer": "https://bid.powerchina.cn/custom", "X-Test": "1"},
    )
    assert headers["Origin"] == "https://bid.powerchina.cn"
    assert headers["Referer"] == "https://bid.powerchina.cn/custom"
    assert headers["X-Test"] == "1"
    assert "Content-Type" in headers


def test_resolve_http_proxy_from_settings():
    with patch("src.core.http_api_client.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            https_proxy="http://proxy.local:8080",
            http_proxy=None,
        )
        assert resolve_http_proxy() == "http://proxy.local:8080"


def test_resolve_http_proxy_falls_back_to_env(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    with patch("src.core.http_api_client.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(https_proxy=None, http_proxy=None)
        assert resolve_http_proxy() == "http://127.0.0.1:7890"


def test_format_http_api_error_adds_proxy_hint():
    exc = httpx.HTTPStatusError(
        "500",
        request=httpx.Request("POST", "https://bid.powerchina.cn/api"),
        response=httpx.Response(500),
    )
    msg = format_http_api_error(exc)
    assert "HTTP_PROXY" in msg or "代理" in msg


@pytest.mark.asyncio
async def test_fetch_api_json_passes_api_headers():
    api = ListPageApi(
        url="https://example.com/list",
        method="POST",
        body_format="json",
        headers={"Referer": "https://example.com/page"},
    )
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"rows": []}

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("src.core.http_api_client.create_http_client", return_value=mock_client):
        from src.core.http_api_client import fetch_api_json

        await fetch_api_json(api, page_param="pageNum", page_num=1, entry_url="https://example.com")

    call_kwargs = mock_client.post.call_args.kwargs
    assert call_kwargs["headers"]["Referer"] == "https://example.com/page"
