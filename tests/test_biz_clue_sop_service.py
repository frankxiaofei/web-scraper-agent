"""商机配置 SOP 服务单元测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web.biz_clue_sop_service import (
    BizClueSopService,
    apply_sop_to_biz_clue_sources,
    generate_keywords_for_domain,
    load_domain_templates,
    match_sites_for_keywords,
    score_site_for_keywords,
)


@pytest.fixture
def sop_tmp_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True)
    sources_path = tmp_path / "biz_clue_sources.yaml"
    sources_path.write_text(
        yaml.safe_dump(
            {"search_keywords": ["已有词"], "default_sync_site_ids": ["ccgp_national"]},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.web.biz_clue_sop_service.SOP_SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr("src.web.biz_clue_sop_service.SOURCES_PATH", sources_path)
    return {"sessions_dir": sessions_dir, "sources_path": sources_path}


def test_load_domain_templates():
    templates = load_domain_templates()
    assert "数字农业" in templates
    assert len(templates["数字农业"].get("keywords") or []) >= 5


def test_generate_keywords_for_domain_template():
    result = generate_keywords_for_domain("数字农业")
    assert result["domain"] == "数字农业"
    assert "数字农业" in result["search_keywords"]
    assert "采购意向" in result["search_keywords"]
    assert result["source"] == "template"


def test_generate_keywords_custom_domain():
    result = generate_keywords_for_domain("低空经济")
    assert "低空经济" in result["search_keywords"]


def test_score_site_for_keywords():
    site = {
        "id": "ccgp_national",
        "name": "中国政府采购网",
        "category": "national",
        "enabled": True,
        "notes": "政府采购公告",
    }
    scored = score_site_for_keywords(site, ["政府采购", "数字农业"])
    assert scored["score"] > 0
    assert scored["site_id"] == "ccgp_national"


def test_match_sites_for_keywords():
    matched = match_sites_for_keywords(["政府采购", "公共资源"], domain="数字农业", limit=5)
    assert len(matched) >= 1
    assert matched[0]["score"] >= matched[-1]["score"]


def test_apply_sop_to_biz_clue_sources(sop_tmp_dirs):
    applied = apply_sop_to_biz_clue_sources(
        search_keywords=["智慧农业", "新词"],
        site_ids=["ggzy_national"],
    )
    assert applied["sync_site_count"] >= 2
    data = yaml.safe_load(sop_tmp_dirs["sources_path"].read_text(encoding="utf-8"))
    assert "智慧农业" in data["search_keywords"]
    assert "ggzy_national" in data["default_sync_site_ids"]
    assert "ccgp_national" in data["default_sync_site_ids"]


def test_sop_session_flow(sop_tmp_dirs, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "src.web.biz_clue_sop_service.match_sites_for_keywords",
        lambda *a, **k: [
            {"site_id": "ccgp_national", "name": "中国政府采购网", "score": 30, "category": "national", "region": "—", "enabled": True, "matched_keywords": [], "url": ""},
            {"site_id": "ggzy_national", "name": "全国公共资源", "score": 25, "category": "national", "region": "—", "enabled": True, "matched_keywords": [], "url": ""},
        ],
    )
    monkeypatch.setattr(
        "src.web.biz_clue_sop_service.apply_sop_to_biz_clue_sources",
        lambda **k: {"search_keywords_count": 10, "sync_site_count": 2},
    )
    monkeypatch.setattr(
        "src.web.biz_clue_sop_service.create_chat_scheduled_task",
        lambda **k: {"id": "task-" + (k.get("site_id") or "notify"), "name": k.get("name")},
    )

    svc = BizClueSopService()
    start = svc.start_session(domain="数字农业")
    assert start["ok"] is True
    sid = start["session"]["id"]
    assert sid

    kw = svc.save_keywords(sid, search_keywords=["数字农业", "智慧农业"])
    assert kw["ok"] is True
    assert kw["session"]["step"] >= 2

    sites = svc.match_and_save_sites(sid, selected_site_ids=["ccgp_national"])
    assert sites["ok"] is True
    assert sites["selected_count"] == 1

    tasks = svc.create_crawl_tasks(sid, create_incremental_tasks=True, apply_sources=True)
    assert tasks["ok"] is True
    assert len(tasks["created_task_ids"]) == 1

    notify = svc.configure_notify(sid, task_name="测试日报")
    assert notify["ok"] is True
    assert notify["session"]["status"] == "completed"
    assert notify["notify_task"]["id"]

    loaded = svc.get_session(sid)
    assert loaded["ok"] is True
    assert loaded["session"]["notify"]["task_id"]


def test_sop_api_start_and_get(sop_tmp_dirs):
    from fastapi.testclient import TestClient
    from src.web.app import app

    client = TestClient(app)
    resp = client.post("/api/biz-clue/sop/start", json={"domain": "智慧城市"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    sid = data["session"]["id"]

    get_resp = client.get(f"/api/biz-clue/sop/{sid}")
    assert get_resp.status_code == 200
    assert get_resp.json()["session"]["domain"] == "智慧城市"


def test_insights_config_redirects_to_sop():
    from fastapi.testclient import TestClient
    from src.web.app import app

    client = TestClient(app, follow_redirects=False)
    resp = client.get("/insights/config")
    assert resp.status_code == 302
    assert resp.headers.get("location") == "/insights/sop"


def test_insights_sop_page():
    from fastapi.testclient import TestClient
    from src.web.app import app

    client = TestClient(app)
    resp = client.get("/insights/sop")
    assert resp.status_code == 200
    assert "商机配置 SOP" in resp.text
