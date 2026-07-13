"""Crawl Agent skill REST API 单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web.app import app

SITE_ID = "zycg_national"


@pytest.fixture
def client():
    return TestClient(app)


def test_skills_execute_unknown_tool(client: TestClient):
    resp = client.post(
        "/api/crawl-agent/skills/execute",
        json={"tool": "not_a_real_tool", "arguments": {}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "未知工具" in (data["error"] or "")


def test_skills_execute_resolve_site(client: TestClient):
    with patch("src.web.crawl_agent_tools.resolve_site_id", return_value=SITE_ID), patch(
        "src.web.crawl_agent_tools.get_site_by_id",
        return_value={"id": SITE_ID, "name": "中央政府采购网", "url": "https://example.com"},
    ):
        resp = client.post(
            "/api/crawl-agent/skills/execute",
            json={"tool": "crawl_resolve_site", "arguments": {"query": "zycg"}},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["result"]["site_id"] == SITE_ID
    assert "error" not in data


def test_skills_execute_by_tool_name(client: TestClient):
    with patch("src.web.crawl_agent_tools.CrawlAgentToolExecutor.execute") as mock_exec:
        mock_exec.return_value = (
            {"ok": True, "title": "T", "body": "B", "severity": "info", "delivered": True, "channel": "cli"},
            "notify ok",
        )
        resp = client.post(
            "/api/crawl-agent/skills/crawl_notify_user",
            json={"title": "巡检", "body": "1 站失败", "severity": "warning"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    mock_exec.assert_called_once_with(
        "crawl_notify_user",
        {"title": "巡检", "body": "1 站失败", "severity": "warning"},
    )


def test_skills_execute_request_user_input(client: TestClient, tmp_path: Path):
    with (
        patch("src.core.crawl_agent_pending.get_settings") as pending_settings,
        patch("src.web.crawl_agent_tools.get_site_by_id") as mock_site,
    ):
        settings = pending_settings.return_value
        settings.data_dir = tmp_path
        mock_site.return_value = {"id": SITE_ID, "name": "中央政府采购网"}
        resp = client.post(
            "/api/crawl-agent/skills/execute",
            json={
                "tool": "crawl_request_user_input",
                "arguments": {
                    "site_id": SITE_ID,
                    "input_type": "login_cookie",
                    "prompt": "请粘贴 Cookie",
                },
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["result"]["pending_id"]
    assert data["result"]["item"]["status"] == "pending"


def test_skills_execute_close_session(client: TestClient):
    with patch("src.web.crawl_agent_tools.CrawlAgentToolExecutor.close_skill_session") as mock_close:
        resp = client.post(
            "/api/crawl-agent/skills/skill_close_session",
            json={"site_id": SITE_ID},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["result"]["closed"] is True
    mock_close.assert_called_once_with(SITE_ID, None)


def test_skills_execute_empty_tool_rejected(client: TestClient):
    resp = client.post("/api/crawl-agent/skills/execute", json={"tool": "", "arguments": {}})
    assert resp.status_code == 400
