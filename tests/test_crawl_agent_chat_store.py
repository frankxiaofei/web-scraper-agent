"""Crawl Agent 对话持久化单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.crawl_agent_chat_store import (
    append_turn,
    create_session,
    delete_all_sessions,
    delete_session,
    get_session,
    list_sessions,
    messages_to_history,
    summarize_session,
)
from src.web.app import app


@pytest.fixture
def chat_store(tmp_path: Path):
    with patch("src.core.crawl_agent_chat_store.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.data_dir = tmp_path
        yield tmp_path / "crawl_agent_chats"


def test_create_and_get_session(chat_store: Path):
    session = create_session(title="测试会话")
    assert session["session_id"]
    assert session["title"] == "测试会话"
    assert session["agent_profile"] == "default"
    assert session["messages"] == []
    assert (chat_store / "default" / f"{session['session_id']}.json").is_file()

    loaded = get_session(session["session_id"])
    assert loaded is not None
    assert loaded["session_id"] == session["session_id"]


def test_append_turn_and_history(chat_store: Path):
    session = create_session()
    sid = session["session_id"]
    append_turn(
        sid,
        user_message="帮我把 tjbid 爬下来",
        blocks=[
            {"type": "plan", "content": "先检查状态"},
            {"type": "tool", "name": "crawl_get_task_status", "args": {"site_id": "tjbid"}},
        ],
        assistant_content="已完成爬取。",
    )
    loaded = get_session(sid)
    assert loaded is not None
    assert len(loaded["messages"]) == 2
    assert loaded["messages"][0]["role"] == "user"
    assert loaded["messages"][1]["role"] == "assistant"
    assert len(loaded["messages"][1]["blocks"]) == 2
    assert loaded["title"] == "帮我把 tjbid 爬下来"

    history = messages_to_history(loaded["messages"])
    assert history == [
        {"role": "user", "content": "帮我把 tjbid 爬下来"},
        {"role": "assistant", "content": "已完成爬取。"},
    ]


def test_list_and_delete_sessions(chat_store: Path):
    s1 = create_session(title="A")
    s2 = create_session(title="B")
    items = list_sessions(limit=10)
    assert len(items) == 2
    assert items[0]["session_id"] in (s1["session_id"], s2["session_id"])
    assert all("message_count" in i for i in items)

    assert delete_session(s1["session_id"]) is True
    assert get_session(s1["session_id"]) is None
    assert (chat_store / "default" / f"{s1['session_id']}.json").is_file() is False
    assert delete_session("not-a-uuid") is False
    assert delete_session(s2["session_id"]) is True
    assert list_sessions() == []


def test_delete_all_sessions(chat_store: Path):
    create_session(title="A")
    create_session(title="B")
    assert len(list_sessions()) == 2
    assert delete_all_sessions() == 2
    assert list_sessions() == []
    assert delete_all_sessions() == 0


def test_summarize_session(chat_store: Path):
    session = create_session()
    summary = summarize_session(session)
    assert summary["session_id"] == session["session_id"]
    assert summary["message_count"] == 0
    assert summary["agent_profile"] == "default"


def test_profile_isolation(chat_store: Path):
    default_sess = create_session(title="主站", agent_profile="default")
    agri_sess = create_session(title="农业", agent_profile="agri")
    stock_sess = create_session(title="股票", agent_profile="stock")

    assert len(list_sessions(agent_profile="default")) == 1
    assert list_sessions(agent_profile="default")[0]["session_id"] == default_sess["session_id"]
    assert len(list_sessions(agent_profile="agri")) == 1
    assert list_sessions(agent_profile="agri")[0]["session_id"] == agri_sess["session_id"]
    assert len(list_sessions(agent_profile="stock")) == 1
    assert list_sessions(agent_profile="stock")[0]["session_id"] == stock_sess["session_id"]

    assert get_session(agri_sess["session_id"], agent_profile="default") is None
    assert get_session(default_sess["session_id"], agent_profile="agri") is None
    assert (chat_store / "agri" / f"{agri_sess['session_id']}.json").is_file()
    assert (chat_store / "stock" / f"{stock_sess['session_id']}.json").is_file()

    assert delete_all_sessions(agent_profile="agri") == 1
    assert list_sessions(agent_profile="agri") == []
    assert len(list_sessions(agent_profile="default")) == 1
    assert len(list_sessions(agent_profile="stock")) == 1


def test_api_chats_crud(chat_store: Path):
    client = TestClient(app)

    empty = client.get("/api/crawl-agent/chats")
    assert empty.status_code == 200
    assert empty.json()["count"] == 0

    created = client.post("/api/crawl-agent/chats", json={"title": "API 测试"})
    assert created.status_code == 200
    sid = created.json()["session"]["session_id"]

    listed = client.get("/api/crawl-agent/chats")
    assert listed.status_code == 200
    assert listed.json()["count"] == 1

    detail = client.get(f"/api/crawl-agent/chats/{sid}")
    assert detail.status_code == 200
    assert detail.json()["title"] == "API 测试"

    missing = client.get("/api/crawl-agent/chats/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404

    deleted = client.delete(f"/api/crawl-agent/chats/{sid}")
    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True
    assert client.get(f"/api/crawl-agent/chats/{sid}").status_code == 404

    missing_delete = client.delete("/api/crawl-agent/chats/00000000-0000-0000-0000-000000000000")
    assert missing_delete.status_code == 404

    invalid_delete = client.delete("/api/crawl-agent/chats/not-a-uuid")
    assert invalid_delete.status_code == 404


def test_api_delete_all_chats(chat_store: Path):
    client = TestClient(app)
    create_session(title="X")
    create_session(title="Y")

    resp = client.delete("/api/crawl-agent/chats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["deleted"] == 2
    assert client.get("/api/crawl-agent/chats").json()["count"] == 0


def test_api_chat_persists_after_stream(chat_store: Path):
    mock_llm = MagicMock()
    mock_llm.available = False

    mock_executor = MagicMock()
    mock_executor.execute.side_effect = [
        (
            {"ok": True, "site": {"site_id": "tjbid", "is_running": False, "sync_stale": False}},
            "status ok",
        ),
        ({"ok": True, "has_rule": True}, "rule ok"),
        ({"ok": True}, "triggered"),
        ({"ok": True}, "poll done"),
    ]

    with patch("src.web.crawl_agent_chat_service.create_chat_client", return_value=mock_llm), patch(
        "src.web.crawl_agent_chat_service.CrawlAgentToolExecutor", return_value=mock_executor
    ), patch("src.web.crawl_agent_tools.resolve_site_id", return_value="tjbid"):
        client = TestClient(app)
        session = create_session()
        sid = session["session_id"]

        resp = client.post(
            "/api/crawl-agent/chat",
            json={"message": "爬取 tjbid", "session_id": sid},
        )
        assert resp.status_code == 200
        assert sid in resp.text

        loaded = get_session(sid)
        assert loaded is not None
        assert len(loaded["messages"]) == 2
        assert loaded["messages"][0]["content"] == "爬取 tjbid"
        assert loaded["messages"][1]["blocks"]
