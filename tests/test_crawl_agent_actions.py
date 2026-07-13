"""Crawl Agent 动作审计 API 单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.crawl_agent_actions import append_action, list_actions, recent_by_site
from src.web.app import app

SITE_ID = "zycg_national"


@pytest.fixture
def actions_store(tmp_path: Path):
    with patch("src.core.crawl_agent_actions.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.data_dir = tmp_path
        yield tmp_path / "crawl_agent_actions.jsonl"


def test_has_action_and_append_if_absent(actions_store: Path):
    from src.core.crawl_agent_actions import append_action_if_absent, has_action

    assert not has_action(
        site_id=SITE_ID,
        action="pending_resolve_credentials",
        detail_contains="pending_id=abc",
    )
    rec = append_action_if_absent(
        site_id=SITE_ID,
        action="pending_resolve_credentials",
        detail="pending_id=abc; cookie_count=2",
        source="hermes",
        dedup_detail_contains="pending_id=abc",
    )
    assert rec is not None
    dup = append_action_if_absent(
        site_id=SITE_ID,
        action="pending_resolve_credentials",
        detail="pending_id=abc; retry",
        source="web",
        dedup_detail_contains="pending_id=abc",
    )
    assert dup is None
    assert len(list_actions(site_id=SITE_ID)) == 1


def test_append_and_list_actions(actions_store: Path):
    rec = append_action(
        site_id=SITE_ID,
        action="spawn_hermes",
        detail="failed,stale_sync",
        source="daemon",
    )
    assert rec["site_id"] == SITE_ID
    assert rec["source"] == "daemon"

    all_items = list_actions(limit=10)
    assert len(all_items) == 1
    assert all_items[0]["action"] == "spawn_hermes"

    filtered = list_actions(site_id=SITE_ID, limit=5)
    assert len(filtered) == 1

    empty = list_actions(site_id="nonexistent", limit=5)
    assert empty == []


def test_recent_by_site(actions_store: Path):
    append_action(site_id=SITE_ID, action="a1", source="daemon")
    append_action(site_id=SITE_ID, action="a2", source="daemon")
    append_action(site_id=SITE_ID, action="a3", source="daemon")
    append_action(site_id=SITE_ID, action="a4", source="daemon")
    append_action(site_id="other_site", action="b1", source="web")

    by_site = recent_by_site(per_site=3)
    assert len(by_site[SITE_ID]) == 3
    assert by_site[SITE_ID][0]["action"] == "a4"
    assert len(by_site["other_site"]) == 1


def test_api_actions_crud(actions_store: Path):
    client = TestClient(app)

    empty = client.get("/api/crawl-agent/actions")
    assert empty.status_code == 200
    assert empty.json()["count"] == 0

    bad_site = client.post(
        "/api/crawl-agent/actions",
        json={
            "site_id": "nonexistent_site_xyz",
            "action": "test",
            "source": "web",
        },
    )
    assert bad_site.status_code == 404

    created = client.post(
        "/api/crawl-agent/actions",
        json={
            "site_id": SITE_ID,
            "action": "reset_stale",
            "detail": "manual reset",
            "source": "web",
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["ok"] is True
    assert body["item"]["summary"]
    assert "reset_stale" in body["item"]["summary"]

    listed = client.get(f"/api/crawl-agent/actions?site_id={SITE_ID}&limit=5")
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert listed.json()["items"][0]["site_id"] == SITE_ID

    bad_body = client.post(
        "/api/crawl-agent/actions",
        json={"site_id": SITE_ID, "action": "", "source": "web"},
    )
    assert bad_body.status_code == 400


def test_tasks_page_shows_agent_actions(actions_store: Path):
    append_action(
        site_id=SITE_ID,
        action="spawn_hermes",
        detail="failed",
        source="daemon",
    )
    client = TestClient(app)
    page = client.get("/tasks")
    assert page.status_code == 200
    assert "最近 Agent 动作" in page.text
    assert "spawn_hermes" in page.text
