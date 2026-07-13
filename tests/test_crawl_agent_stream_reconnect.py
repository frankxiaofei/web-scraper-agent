"""Hermes Crawl Agent 流式恢复 — run 注册表、partial turn、SSE 重连。"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.core.crawl_agent_chat_store import (
    abort_streaming_turn,
    begin_streaming_turn,
    create_session,
    finalize_interrupted_turn,
    finalize_streaming_turn,
    get_session,
    set_active_hermes_run_id,
    update_streaming_turn,
)
from src.web.app import app
from src.web.crawl_agent_run_registry import (
    clear_run,
    finish_run,
    get_run_info,
    is_run_active,
    publish_event,
    start_run,
    stream_run_events,
)
from src.web.crawl_agent_stream_reconcile import (
    count_buffered_events,
    reconcile_streaming_session,
)


@pytest.fixture
def chat_store(tmp_path: Path):
    with patch("src.core.crawl_agent_chat_store.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.data_dir = tmp_path
        yield tmp_path / "crawl_agent_chats"


@pytest.fixture(autouse=True)
def reset_run_registry():
    tracked: list[str] = []
    yield tracked
    for sid in tracked:
        clear_run(sid)


def test_streaming_turn_lifecycle(chat_store: Path):
    session = create_session()
    sid = session["session_id"]

    begin_streaming_turn(sid, user_message="爬取 tjbid")
    loaded = get_session(sid)
    assert loaded is not None
    assert loaded.get("streaming") is True
    assert len(loaded["messages"]) == 2
    assert loaded["messages"][-1]["streaming"] is True

    update_streaming_turn(
        sid,
        blocks=[{"type": "thinking", "content": "规划中"}],
        assistant_content="",
    )
    loaded = get_session(sid)
    assert loaded["messages"][-1]["blocks"][0]["content"] == "规划中"

    finalize_streaming_turn(
        sid,
        blocks=[
            {"type": "thinking", "content": "规划中"},
            {"type": "result", "name": "x", "summary": "ok"},
        ],
        assistant_content="完成",
    )
    loaded = get_session(sid)
    assert loaded.get("streaming") is None
    assert loaded["messages"][-1].get("streaming") is None
    assert loaded["messages"][-1]["content"] == "完成"


def test_abort_streaming_turn(chat_store: Path):
    session = create_session()
    sid = session["session_id"]
    begin_streaming_turn(sid, user_message="test")
    assert abort_streaming_turn(sid) is True
    loaded = get_session(sid)
    assert loaded is not None
    assert loaded["messages"] == []
    assert loaded.get("streaming") is None


@pytest.mark.asyncio
async def test_run_registry_publish_and_replay(reset_run_registry: list[str]):
    sid = "00000000-0000-0000-0000-000000000001"
    reset_run_registry.append(sid)
    assert start_run(sid, "hello") is True
    assert is_run_active(sid) is True
    assert publish_event(sid, {"type": "ping"}) == 0
    assert publish_event(sid, {"type": "thinking", "content": "a"}) == 1
    assert publish_event(sid, {"type": "tool", "name": "x"}) == 2

    async def collect(after: int = 0) -> list[dict]:
        out = []
        async for ev in stream_run_events(sid, after=after):
            out.append(ev)
        return out

    finish_run(sid, status="completed")
    replay = await collect(after=1)
    assert len(replay) == 1
    assert replay[0]["type"] == "tool"

    assert is_run_active(sid) is False
    info = get_run_info(sid)
    assert info is not None
    assert info["event_count"] == 2


def test_api_rejects_duplicate_when_run_active(chat_store: Path, reset_run_registry: list[str]):
    session = create_session()
    sid = session["session_id"]
    reset_run_registry.append(sid)
    begin_streaming_turn(sid, user_message="进行中")
    assert start_run(sid, "进行中") is True

    client = TestClient(app)
    resp = client.post(
        "/api/crawl-agent/chat",
        json={"message": "重复请求", "session_id": sid},
    )
    assert resp.status_code == 409

    finish_run(sid, status="completed")
    finalize_streaming_turn(sid, blocks=[], assistant_content="done")


def test_api_get_session_includes_run_status(chat_store: Path, reset_run_registry: list[str]):
    session = create_session()
    sid = session["session_id"]
    reset_run_registry.append(sid)
    start_run(sid, "running msg")
    publish_event(sid, {"type": "thinking", "content": "x"})

    client = TestClient(app)
    resp = client.get(f"/api/crawl-agent/chats/{sid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run"]["status"] == "running"
    assert body["run"]["event_count"] == 1

    finish_run(sid, status="completed")


@pytest.mark.asyncio
async def test_stream_run_events_reconnect_after_offset(reset_run_registry: list[str]):
    """after=N 应跳过前 N 个缓冲事件，并继续订阅增量。"""
    sid = "00000000-0000-0000-0000-000000000002"
    reset_run_registry.append(sid)
    assert start_run(sid, "dup") is True
    assert publish_event(sid, {"type": "thinking", "content": "a"}) == 1
    assert publish_event(sid, {"type": "thinking", "content": "b"}) == 2

    collected: list[dict] = []

    async def consumer():
        async for ev in stream_run_events(sid, after=1):
            collected.append(ev)

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.05)
    assert publish_event(sid, {"type": "tool", "name": "x"}) == 3
    finish_run(sid, status="completed")
    await asyncio.wait_for(task, timeout=2)

    assert [e.get("content") for e in collected if e["type"] == "thinking"] == ["b"]
    assert collected[-1]["type"] == "tool"
    assert len(collected) == 2


def test_sse_keepalive_interval_is_15_seconds():
    from src.web import sse_utils

    assert sse_utils.SSE_KEEPALIVE_INTERVAL == 15.0


def test_api_reconnect_stream_while_running(chat_store: Path, reset_run_registry: list[str]):
    session = create_session()
    sid = session["session_id"]
    reset_run_registry.append(sid)
    start_run(sid, "live")
    publish_event(sid, {"type": "thinking", "content": "buffered"})

    done = threading.Event()

    def finish_later():
        time.sleep(0.15)
        publish_event(sid, {"type": "done"})
        finish_run(sid, status="completed")
        done.set()

    threading.Thread(target=finish_later, daemon=True).start()

    client = TestClient(app)
    with client.stream("GET", f"/api/crawl-agent/chats/{sid}/stream?after=0") as recon:
        assert recon.status_code == 200
        text = recon.read().decode("utf-8")
        assert "thinking" in text
        assert "buffered" in text

    done.wait(timeout=2)


def test_finalize_interrupted_turn(chat_store: Path):
    session = create_session()
    sid = session["session_id"]
    begin_streaming_turn(sid, user_message="test")
    update_streaming_turn(
        sid,
        blocks=[{"type": "thinking", "content": "partial"}],
    )
    set_active_hermes_run_id(sid, "run_test123")

    finalize_interrupted_turn(sid, reason="连接中断")
    loaded = get_session(sid)
    assert loaded is not None
    assert loaded.get("streaming") is None
    assert loaded.get("active_hermes_run_id") is None
    blocks = loaded["messages"][-1]["blocks"]
    assert any(b.get("data", {}).get("interrupted") for b in blocks)


def test_reconcile_stale_streaming_finalizes(chat_store: Path):
    session = create_session()
    sid = session["session_id"]
    begin_streaming_turn(sid, user_message="stale")
    loaded = get_session(sid)
    assert loaded is not None
    old = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
    loaded["updated_at"] = old
    from src.core.crawl_agent_chat_store import _save_session

    _save_session(loaded)

    reconciled = reconcile_streaming_session(loaded, stale_minutes=30)
    assert reconciled.get("streaming") is None
    refreshed = get_session(sid)
    assert refreshed is not None
    assert refreshed.get("streaming") is None


def test_reconcile_orphan_without_hermes_run_id_finalizes(chat_store: Path):
    session = create_session()
    sid = session["session_id"]
    begin_streaming_turn(sid, user_message="orphan")
    update_streaming_turn(
        sid,
        blocks=[{"type": "thinking", "content": "partial"}],
    )
    loaded = get_session(sid)
    assert loaded is not None

    reconciled = reconcile_streaming_session(loaded, stale_minutes=30)
    assert reconciled.get("streaming") is None


def test_count_buffered_events_from_blocks(chat_store: Path):
    session = create_session()
    sid = session["session_id"]
    begin_streaming_turn(sid, user_message="count")
    update_streaming_turn(
        sid,
        blocks=[
            {"type": "thinking", "content": "a"},
            {"type": "tool", "name": "x"},
            {"type": "ping", "content": "ignored"},
        ],
    )
    loaded = get_session(sid)
    assert loaded is not None
    assert count_buffered_events(loaded) == 2


def test_reconcile_resumes_terminal_hermes_run(chat_store: Path, reset_run_registry: list[str]):
    session = create_session()
    sid = session["session_id"]
    reset_run_registry.append(sid)
    begin_streaming_turn(sid, user_message="resume")
    update_streaming_turn(
        sid,
        blocks=[{"type": "thinking", "content": "partial"}],
    )
    set_active_hermes_run_id(sid, "run_terminal")
    loaded = get_session(sid)
    assert loaded is not None

    class _FakeClient:
        available = True

        def get_run_status_sync(self, run_id: str) -> str:
            assert run_id == "run_terminal"
            return "completed"

    with patch(
        "src.core.hermes_agent_chat_client.get_hermes_agent_chat_client",
        return_value=_FakeClient(),
    ), patch("src.web.crawl_agent_stream_reconcile._spawn_hermes_relay", return_value=True) as mock_spawn:
        reconciled = reconcile_streaming_session(loaded, stale_minutes=30)

    assert reconciled.get("streaming") is True
    mock_spawn.assert_called_once()


def test_api_get_session_reconciles_orphan(chat_store: Path):
    session = create_session()
    sid = session["session_id"]
    begin_streaming_turn(sid, user_message="orphan")
    loaded = get_session(sid)
    assert loaded is not None
    old = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
    loaded["updated_at"] = old
    from src.core.crawl_agent_chat_store import _save_session

    _save_session(loaded)

    client = TestClient(app)
    resp = client.get(f"/api/crawl-agent/chats/{sid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("streaming") is None
    assert "run" not in body or body.get("run", {}).get("status") != "running"
