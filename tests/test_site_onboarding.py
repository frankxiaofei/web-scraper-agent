"""站点 onboarding MVP 测试。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.site_onboarding import (
    ProbeReport,
    get_onboarding_status,
    register_site,
    save_probe_report,
)
from src.web.app import app
from src.web.browser_crawl import infer_site_id_from_url
from src.web.crawl_agent_tools import resolve_site_id


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def isolated_onboarding(tmp_path, monkeypatch):
    drafts = tmp_path / "sites_drafts"
    onboarding = tmp_path / "site_onboarding"
    drafts.mkdir()
    onboarding.mkdir()
    monkeypatch.setattr("src.core.site_onboarding.DRAFTS_DIR", drafts)
    monkeypatch.setattr("src.core.site_onboarding.ONBOARDING_DIR", onboarding)
    return drafts, onboarding


def test_register_site_writes_draft_and_status(isolated_onboarding):
    drafts, onboarding_dir = isolated_onboarding
    result = register_site("测试站点", "example-test.invalid")
    site_id = result["site_id"]
    assert site_id
    assert (drafts / f"{site_id}.yaml").is_file()
    assert (onboarding_dir / f"{site_id}.json").is_file()
    assert result["stages"]["register"] == "completed"
    assert result["current_stage"] == "probe"
    assert result["probe_report"]["status"] == "pending"


def test_get_onboarding_status_from_json(isolated_onboarding):
    data = register_site("探查站", "probe.example.com")
    status = get_onboarding_status(data["site_id"])
    assert status is not None
    assert status.name == "探查站"
    assert status.stages["probe"] == "in_progress"


def test_save_probe_report_advances_stage(isolated_onboarding):
    data = register_site("分析站", "analyze.example.com")
    site_id = data["site_id"]
    report = ProbeReport(
        site_id=site_id,
        url=data["url"],
        status="completed",
        summary="stub probe ok",
        hints={"adapter": "generic"},
    )
    updated = save_probe_report(site_id, report)
    assert updated.stages["probe"] == "completed"
    assert updated.stages["analyze"] == "in_progress"
    assert updated.current_stage == "analyze"


def test_api_sites_register(client, isolated_onboarding):
    resp = client.post(
        "/api/sites/register",
        json={"name": "API 注册站", "url": "https://api-register.example.com/"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["site_id"]
    assert data["stages"]["register"] == "completed"


def test_api_onboarding_status(client, isolated_onboarding):
    reg = client.post(
        "/api/sites/register",
        json={"name": "状态站", "url": "status.example.com"},
    ).json()
    site_id = reg["site_id"]
    resp = client.get(f"/api/sites/onboarding/{site_id}/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["site_id"] == site_id
    assert "stage_labels" in body
    assert body["stage_labels"]["probe"] == "探查"


def test_api_onboarding_status_not_found(client):
    resp = client.get("/api/sites/onboarding/no_such_site_xyz/status")
    assert resp.status_code == 404


def test_api_sites_resolve_bare_domain(client):
    resp = client.get("/api/sites/resolve", params={"query": "bid.powerchina.cn"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["site_id"] == "中国电力建设集团有限公司_公共资源交易服务平台"


def test_resolve_site_id_bare_domain():
    site_id = resolve_site_id("bid.powerchina.cn")
    assert site_id == "中国电力建设集团有限公司_公共资源交易服务平台"


def test_infer_site_id_from_bare_domain():
    site_id = infer_site_id_from_url("bid.powerchina.cn")
    assert site_id == "中国电力建设集团有限公司_公共资源交易服务平台"


def test_infer_site_enhanced_api(client):
    resp = client.post("/api/browser/infer-site", json={"url": "bid.powerchina.cn"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["registered"] is True
    assert data["site_id"] == "中国电力建设集团有限公司_公共资源交易服务平台"


def test_onboard_redirects_to_hermes(tmp_path):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = tmp_path

        import src.web.data_service as data_mod

        data_mod._service = None
        client = TestClient(app, follow_redirects=False)
        resp = client.get("/onboard")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/hermes?intent=onboard"
        resp2 = client.get("/onboard/test_site_id")
        assert resp2.status_code == 302
        assert "intent=onboard" in resp2.headers["location"]
        assert "site_id=test_site_id" in resp2.headers["location"]
        data_mod._service = None
