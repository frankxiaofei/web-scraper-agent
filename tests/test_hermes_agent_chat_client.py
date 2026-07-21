"""Hermes Agent Chat Client 单元测试。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.core.hermes_agent_chat_client import (
    HermesAgentChatClient,
    _auth_headers,
    resolve_hermes_agent_chat_url,
)


def test_resolve_chat_url_from_explicit():
    settings = MagicMock()
    settings.hermes_agent_chat_url = "http://hermes:8642"
    settings.hermes_agent_url = "http://hermes:8080"
    assert resolve_hermes_agent_chat_url(settings) == "http://hermes:8642"


def test_resolve_chat_url_from_dispatch_8080():
    settings = MagicMock()
    settings.hermes_agent_chat_url = None
    settings.hermes_agent_url = "http://hermes-agent:8080"
    assert resolve_hermes_agent_chat_url(settings) == "http://hermes-agent:8642"


def test_resolve_chat_url_empty():
    settings = MagicMock()
    settings.hermes_agent_chat_url = None
    settings.hermes_agent_url = None
    assert resolve_hermes_agent_chat_url(settings) == ""


def test_auth_headers_omits_bearer_when_key_missing():
    settings = MagicMock()
    settings.hermes_agent_api_key = "   "
    headers = _auth_headers(settings)
    assert "Authorization" not in headers


def test_auth_headers_includes_bearer_when_key_set():
    settings = MagicMock()
    settings.hermes_agent_api_key = "test-key"
    headers = _auth_headers(settings)
    assert headers["Authorization"] == "Bearer test-key"


@pytest.mark.asyncio
async def test_start_run_passes_session_id():
    client = HermesAgentChatClient(base_url="http://hermes.test:8642")

    mock_resp = MagicMock()
    mock_resp.status_code = 202
    mock_resp.json.return_value = {"run_id": "run_abc", "status": "started"}

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        run_id, err = await client.start_run(
            user_message="hello",
            session_id="sess-uuid-123",
        )

    assert run_id == "run_abc"
    assert err is None
    call_kwargs = mock_client.post.await_args.kwargs
    assert call_kwargs["json"]["session_id"] == "sess-uuid-123"


@pytest.mark.asyncio
async def test_start_run_success():
    client = HermesAgentChatClient(base_url="http://hermes.test:8642")

    mock_resp = MagicMock()
    mock_resp.status_code = 202
    mock_resp.json.return_value = {"run_id": "run_abc", "status": "started"}

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        run_id, err = await client.start_run(user_message="hello")

    assert run_id == "run_abc"
    assert err is None
    mock_client.post.assert_awaited_once()
    call_kwargs = mock_client.post.await_args.kwargs
    assert call_kwargs["json"]["input"] == "hello"


@pytest.mark.asyncio
async def test_start_run_http_error():
    client = HermesAgentChatClient(base_url="http://hermes.test:8642")

    mock_resp = MagicMock()
    mock_resp.status_code = 503
    mock_resp.text = "unavailable"

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        run_id, err = await client.start_run(user_message="hello")

    assert run_id is None
    assert "503" in (err or "")


@pytest.mark.asyncio
async def test_stream_run_events_parses_sse():
    from contextlib import asynccontextmanager

    client = HermesAgentChatClient(base_url="http://hermes.test:8642")

    lines = [
        'data: {"event":"tool.started","tool":"crawl_list_sites"}',
        'data: {"event":"run.completed","output":"done"}',
    ]

    async def _aiter_lines():
        for line in lines:
            yield line

    mock_stream_resp = MagicMock()
    mock_stream_resp.status_code = 200
    mock_stream_resp.aiter_lines = _aiter_lines

    @asynccontextmanager
    async def _stream(*_a, **_k):
        yield mock_stream_resp

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.stream = _stream
        mock_client_cls.return_value = mock_client

        events = [e async for e in client.stream_run_events("run_abc")]

    assert events[0]["event"] == "tool.started"
    assert events[1]["event"] == "run.completed"
