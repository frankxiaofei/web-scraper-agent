"""Crawl Agent 对话中止机制测试。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.web.app import app
from src.web.crawl_agent_cancel import (
    cancel_chat_session,
    is_chat_cancelled,
    register_chat_session,
    set_thread_chat_session,
    unregister_chat_session,
)
from src.web.crawl_agent_chat_service import ChatStreamEvent, CrawlAgentChatService
from src.web.crawl_agent_tools import CrawlAgentToolExecutor


async def _collect_events(service: CrawlAgentChatService, message: str, **kwargs) -> list[ChatStreamEvent]:
    events: list[ChatStreamEvent] = []
    async for ev in service.stream_chat(message, **kwargs):
        events.append(ev)
    return events


def test_register_and_cancel_chat_session():
    sid = "sess_test_1"
    ev = register_chat_session(sid)
    assert not ev.is_set()
    assert not is_chat_cancelled(sid)

    result = cancel_chat_session(sid)
    assert result["ok"] is True
    assert ev.is_set()
    assert is_chat_cancelled(sid)

    unregister_chat_session(sid)
    assert not is_chat_cancelled(sid)


def test_cancel_unknown_session():
    result = cancel_chat_session("nonexistent_session_xyz")
    assert result["ok"] is False


def test_tool_executor_returns_cancelled_when_flag_set():
    sid = "sess_tool_cancel"
    ev = register_chat_session(sid)
    set_thread_chat_session(sid)
    ev.set()
    try:
        executor = CrawlAgentToolExecutor()
        result, summary = executor.execute("crawl_list_sites", {})
        assert result.get("cancelled") is True
        assert summary == "已中止"
    finally:
        set_thread_chat_session(None)
        unregister_chat_session(sid)


@pytest.mark.asyncio
async def test_stream_chat_yields_cancelled_on_flag():
    mock_llm = MagicMock()
    mock_llm.available = True

    def slow_chat(*_args, **_kwargs):
        time.sleep(0.05)
        return (
            {
                "role": "assistant",
                "content": "继续",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "crawl_list_sites",
                            "arguments": "{}",
                        },
                    }
                ],
            },
            None,
        )

    mock_llm.chat_completion.side_effect = slow_chat

    sid = "sess_stream_cancel"
    cancel_event = register_chat_session(sid)

    async def cancel_after_delay() -> None:
        await asyncio.sleep(0.02)
        cancel_event.set()

    service = CrawlAgentChatService(llm=mock_llm, max_turns=5)
    cancel_task = asyncio.create_task(cancel_after_delay())
    try:
        events = await _collect_events(
            service,
            "爬取 tjbid",
            session_id=sid,
            cancel_event=cancel_event,
        )
    finally:
        await cancel_task
        unregister_chat_session(sid)

    assert any(e.type == "cancelled" for e in events)
    assert events[-1].type == "done"


def test_cancel_api_by_path():
    sid = "sess_api_cancel_path"
    register_chat_session(sid)
    try:
        client = TestClient(app)
        resp = client.post(f"/api/crawl-agent/chat/{sid}/cancel")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["cancelled"] is True
    finally:
        unregister_chat_session(sid)


def test_cancel_api_by_body():
    sid = "sess_api_cancel_body"
    register_chat_session(sid)
    try:
        client = TestClient(app)
        resp = client.post("/api/crawl-agent/cancel", json={"session_id": sid})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
    finally:
        unregister_chat_session(sid)


def test_cancel_api_rejects_empty_session_id():
    client = TestClient(app)
    resp = client.post("/api/crawl-agent/cancel", json={"session_id": ""})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_stream_chat_tool_result_cancelled():
    mock_llm = MagicMock()
    mock_llm.available = True
    mock_llm.chat_completion.return_value = (
        {
            "role": "assistant",
            "content": "执行工具",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "skill_fetch_list_page",
                        "arguments": json.dumps({"site_id": "demo", "page_num": 1}),
                    },
                }
            ],
        },
        None,
    )

    mock_executor = MagicMock(spec=CrawlAgentToolExecutor)
    mock_executor.execute.return_value = (
        {"ok": False, "cancelled": True, "error": "用户已中止爬取"},
        "已中止",
    )

    sid = "sess_tool_result_cancel"
    register_chat_session(sid)
    try:
        service = CrawlAgentChatService(
            llm=mock_llm,
            tool_executor=mock_executor,
            max_turns=3,
        )
        events = await _collect_events(
            service,
            "爬取 demo",
            session_id=sid,
        )
    finally:
        unregister_chat_session(sid)

    assert any(e.type == "cancelled" for e in events)
    assert events[-1].type == "done"
