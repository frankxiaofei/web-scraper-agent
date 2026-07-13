"""SSE keepalive 与长连接相关测试。"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.core.crawl_agent_chat_store import create_session
from src.web.app import app
from src.web.crawl_agent_chat_service import ChatStreamEvent, CrawlAgentChatService
from src.web.sse_utils import (
    SSE_KEEPALIVE_INTERVAL,
    SSE_PING_COMMENT,
    iter_with_idle_keepalive,
    yield_pings_while_waiting,
)



@pytest.mark.asyncio
async def test_iter_with_idle_keepalive_emits_ping():
    async def slow_source():
        await asyncio.sleep(0.25)
        yield ChatStreamEvent(type="thinking", content="ok")

    started = time.monotonic()
    items: list = []
    async for item in iter_with_idle_keepalive(slow_source(), interval=0.1):
        items.append(item)
    elapsed = time.monotonic() - started
    assert any(item == SSE_PING_COMMENT for item in items)
    assert any(isinstance(item, ChatStreamEvent) for item in items)
    assert elapsed >= 0.1


@pytest.mark.asyncio
async def test_yield_pings_while_waiting():
    async def slow():
        await asyncio.sleep(0.35)
        return "done"

    task = asyncio.create_task(slow())
    pings = []
    async for ping in yield_pings_while_waiting(task, interval=0.1):
        pings.append(ping)
    assert len(pings) >= 2
    assert task.result() == "done"


@pytest.mark.asyncio
async def test_stream_chat_yields_ping_during_slow_tool():
    mock_llm = MagicMock()
    mock_llm.available = True
    mock_llm.chat_completion.return_value = (
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "function": {"name": "crawl_resolve_site", "arguments": '{"query":"tjbid"}'},
                }
            ],
        },
        None,
    )

    mock_executor = MagicMock()

    def slow_execute(name, args):
        time.sleep(0.35)
        return {"ok": True, "site_id": "tjbid"}, "resolved"

    mock_executor.execute.side_effect = slow_execute

    with patch("src.web.crawl_agent_chat_service.create_chat_client", return_value=mock_llm), patch(
        "src.web.sse_utils.SSE_KEEPALIVE_INTERVAL", 0.1
    ):
        service = CrawlAgentChatService(llm=mock_llm, tool_executor=mock_executor)
        events = []
        async for ev in service.stream_chat("tjbid"):
            events.append(ev)

    assert any(e.type == "ping" for e in events)
    assert events[-1].type == "done"


def test_api_chat_sse_includes_keepalive_ping():
    mock_llm = MagicMock()
    mock_llm.available = True

    call_count = {"n": 0}

    def slow_completion(*_args, **_kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            time.sleep(0.35)
            return (
                {
                    "role": "assistant",
                    "content": "hi",
                    "tool_calls": [],
                },
                None,
            )
        return ({"role": "assistant", "content": "done", "tool_calls": []}, None)

    mock_llm.chat_completion.side_effect = slow_completion

    with patch("src.web.crawl_agent_chat_service.create_chat_client", return_value=mock_llm), patch(
        "src.web.sse_utils.SSE_KEEPALIVE_INTERVAL", 0.1
    ):
        client = TestClient(app)
        session = create_session()
        resp = client.post(
            "/api/crawl-agent/chat",
            json={"message": "hello", "session_id": session["session_id"]},
        )
        assert resp.status_code == 200
        assert ": ping" in resp.text or '"type":"ping"' in resp.text
