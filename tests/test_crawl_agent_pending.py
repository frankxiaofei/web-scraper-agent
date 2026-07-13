"""Crawl Agent 待办队列 API 单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.crawl_agent_actions import append_action_if_absent, list_actions
from src.core.crawl_agent_pending import (
    create_pending_item,
    list_pending_items,
    resolve_pending_item,
)
from src.web.app import app

SITE_ID = "zycg_national"
SECRET_COOKIE = "session=super-secret-cookie-value"


@pytest.fixture
def pending_store(tmp_path: Path):
    with patch("src.core.crawl_agent_pending.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.data_dir = tmp_path
        yield tmp_path / "crawl_agent_pending.json"


@pytest.fixture
def pending_and_credentials(tmp_path: Path):
    with (
        patch("src.core.crawl_agent_pending.get_settings") as pending_settings,
        patch("src.core.site_credentials.get_settings") as cred_settings,
        patch("src.core.crawl_agent_actions.get_settings") as actions_settings,
    ):
        for mock in (pending_settings, cred_settings, actions_settings):
            settings = mock.return_value
            settings.data_dir = tmp_path
            settings.site_credentials_key = "test-credentials-key"
        yield tmp_path


def test_create_and_list_pending(pending_store: Path):
    item = create_pending_item(
        site_id=SITE_ID,
        input_type="login_cookie",
        prompt="请粘贴 Cookie",
        context={"run_id": "run-1"},
    )
    assert item["status"] == "pending"
    assert item["input_type_label"] == "登录 Cookie"

    items = list_pending_items()
    assert len(items) == 1
    assert items[0]["id"] == item["id"]


def test_list_pending_filters_by_site_id(pending_store: Path):
    create_pending_item(site_id=SITE_ID, input_type="captcha", prompt="A")
    create_pending_item(site_id="other_site", input_type="captcha", prompt="B")

    all_pending = list_pending_items()
    assert len(all_pending) == 2

    filtered = list_pending_items(site_id=SITE_ID)
    assert len(filtered) == 1
    assert filtered[0]["site_id"] == SITE_ID


def test_resolve_login_cookie_records_action(pending_and_credentials: Path):
    item = create_pending_item(
        site_id=SITE_ID,
        input_type="login_cookie",
        prompt="请粘贴 Cookie",
    )
    result = resolve_pending_item(item["id"], SECRET_COOKIE)
    assert result["credentials_saved"] is True
    assert result.get("action_recorded") is True

    actions = list_actions(site_id=SITE_ID, limit=5)
    assert len(actions) == 1
    assert actions[0]["action"] == "pending_resolve_credentials"
    assert item["id"] in actions[0]["detail"]


def test_resolve_login_cookie_skips_duplicate_action(pending_and_credentials: Path):
    item = create_pending_item(
        site_id=SITE_ID,
        input_type="login_cookie",
        prompt="请粘贴 Cookie",
    )
    append_action_if_absent(
        site_id=SITE_ID,
        action="pending_resolve_credentials",
        detail=f"pending_id={item['id']}; source=hermes",
        source="hermes",
        dedup_detail_contains=f"pending_id={item['id']}",
    )

    result = resolve_pending_item(item["id"], SECRET_COOKIE)
    assert result["credentials_saved"] is True
    assert result.get("action_skipped_duplicate") is True

    actions = list_actions(site_id=SITE_ID, limit=5)
    assert len(actions) == 1


def test_api_pending_site_id_filter(pending_and_credentials: Path):
    client = TestClient(app)
    create_pending_item(site_id=SITE_ID, input_type="captcha", prompt="A")
    create_pending_item(site_id="other_site", input_type="captcha", prompt="B")

    resp = client.get(f"/api/crawl-agent/pending?site_id={SITE_ID}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["items"][0]["site_id"] == SITE_ID


def test_resolve_login_cookie_saves_credentials(pending_and_credentials: Path):
    item = create_pending_item(
        site_id=SITE_ID,
        input_type="login_cookie",
        prompt="请粘贴 Cookie",
    )
    result = resolve_pending_item(item["id"], SECRET_COOKIE)
    assert result["ok"] is True
    assert result["credentials_saved"] is True
    assert result["item"]["status"] == "resolved"
    assert result["item"]["user_response"] == SECRET_COOKIE

    assert list_pending_items() == []
    resolved = list_pending_items(status="resolved")
    assert len(resolved) == 1

    cred_path = pending_and_credentials / "site_credentials" / f"{SITE_ID}.json"
    assert cred_path.is_file()
    assert SECRET_COOKIE not in cred_path.read_text(encoding="utf-8")


def test_resolve_generic_no_credentials(pending_store: Path):
    item = create_pending_item(
        site_id=SITE_ID,
        input_type="rule_confirm",
        prompt="确认保存规则？",
        choices=["是", "否"],
    )
    result = resolve_pending_item(item["id"], "是")
    assert result["ok"] is True
    assert "credentials_saved" not in result


def test_api_pending_crud(pending_and_credentials: Path):
    client = TestClient(app)

    empty = client.get("/api/crawl-agent/pending")
    assert empty.status_code == 200
    assert empty.json()["count"] == 0

    bad_site = client.post(
        "/api/crawl-agent/pending",
        json={
            "site_id": "nonexistent_site_xyz",
            "input_type": "login_cookie",
            "prompt": "test",
        },
    )
    assert bad_site.status_code == 404

    created = client.post(
        "/api/crawl-agent/pending",
        json={
            "site_id": SITE_ID,
            "input_type": "login_cookie",
            "prompt": "请粘贴 yzw.cn Cookie",
            "context": {"last_error": "跳转登录页"},
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["ok"] is True
    item_id = body["item"]["id"]

    listed = client.get("/api/crawl-agent/pending")
    assert listed.status_code == 200
    assert listed.json()["count"] == 1

    resolved = client.post(
        f"/api/crawl-agent/pending/{item_id}/resolve",
        json={"user_response": SECRET_COOKIE},
    )
    assert resolved.status_code == 200
    resolved_body = resolved.json()
    assert resolved_body["credentials_saved"] is True

    again = client.post(
        f"/api/crawl-agent/pending/{item_id}/resolve",
        json={"user_response": "other"},
    )
    assert again.status_code == 409

    missing = client.post(
        "/api/crawl-agent/pending/missing-id/resolve",
        json={"user_response": "x"},
    )
    assert missing.status_code == 404


def test_pending_page_renders(pending_store: Path):
    create_pending_item(
        site_id=SITE_ID,
        input_type="captcha",
        prompt="请输入验证码",
    )
    client = TestClient(app)
    page = client.get("/crawl-agent/pending")
    assert page.status_code == 200
    assert "待办队列" in page.text
    assert SITE_ID in page.text
