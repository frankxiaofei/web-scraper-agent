"""站点删除 API 与 delete_site 单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient


@pytest.fixture
def delete_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rules_dir = config_dir / "crawl_rules"
    rules_dir.mkdir()
    scripts_dir = data_dir / "crawl_scripts"
    scripts_dir.mkdir()
    creds_dir = data_dir / "site_credentials"
    creds_dir.mkdir()
    drafts_dir = config_dir / "sites_drafts"
    drafts_dir.mkdir()
    onboarding_dir = data_dir / "site_onboarding"
    onboarding_dir.mkdir()

    site_id = "demo_site"
    sites_yaml = {
        "crawl_defaults": {"max_crawl_depth": 3},
        "sites": [
            {
                "id": site_id,
                "name": "演示站点",
                "url": "https://demo.example/",
                "adapter": "generic",
                "enabled": True,
            },
            {
                "id": "protected_site",
                "name": "受保护站点",
                "url": "https://protected.example/",
                "adapter": "generic",
                "enabled": True,
                "protected": True,
            },
        ],
    }
    sites_path = config_dir / "sites.yaml"
    sites_path.write_text(yaml.dump(sites_yaml, allow_unicode=True), encoding="utf-8")

    schedule_yaml = {
        "scheduler": {"per_site_schedule_enabled": True},
        "jobs": [
            {"site_id": site_id, "trigger": "interval", "minutes": 30, "enabled": True},
            {"site_id": "other_site", "trigger": "interval", "minutes": 60, "enabled": True},
        ],
    }
    schedule_path = config_dir / "schedule.yaml"
    schedule_path.write_text(yaml.dump(schedule_yaml, allow_unicode=True), encoding="utf-8")

    (rules_dir / f"{site_id}.yaml").write_text(
        yaml.dump(
            {
                "site_id": site_id,
                "enabled": True,
                "list_page": {"strategy": "dom", "item": "a"},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    (scripts_dir / f"{site_id}.json").write_text(
        json.dumps({"site_id": site_id, "script_type": "yaml_rule"}),
        encoding="utf-8",
    )

    (drafts_dir / f"{site_id}.yaml").write_text(
        yaml.dump({"id": site_id, "name": "演示站点"}, allow_unicode=True),
        encoding="utf-8",
    )
    (onboarding_dir / f"{site_id}.json").write_text(
        json.dumps({"site_id": site_id, "name": "演示站点"}),
        encoding="utf-8",
    )

    script_cron_jobs = {
        "version": 1,
        "jobs": [
            {
                "id": "cron-demo",
                "name": "演示定时",
                "script": "run_once.py",
                "args": ["--site-id", site_id],
                "cron": "0 3 * * *",
                "enabled": True,
            },
            {
                "id": "cron-other",
                "name": "其他定时",
                "script": "run_once.py",
                "args": ["--site-id", "other_site"],
                "cron": "0 4 * * *",
                "enabled": True,
            },
        ],
    }
    (data_dir / "script_cron_jobs.json").write_text(
        json.dumps(script_cron_jobs), encoding="utf-8"
    )

    monkeypatch.setattr("src.core.site_sync.SITES_PATH", sites_path)
    monkeypatch.setattr("src.core.site_sync.SCHEDULE_PATH", schedule_path)
    monkeypatch.setattr("src.core.site_config_write.SITES_PATH", sites_path)
    monkeypatch.setattr("src.core.site_config_write.SCHEDULE_PATH", schedule_path)
    monkeypatch.setattr("src.core.site_config_write.CRAWL_SCRIPTS_DIR", scripts_dir)
    monkeypatch.setattr("src.core.crawl_rules.DEFAULT_RULES_DIR", rules_dir)
    monkeypatch.setattr("src.core.crawl_rules._default_loader", None)
    monkeypatch.setattr("src.core.script_cron.JOBS_PATH", data_dir / "script_cron_jobs.json")
    monkeypatch.setattr("src.core.script_cron.DATA_DIR", data_dir)
    monkeypatch.setattr("src.core.site_onboarding.DRAFTS_DIR", drafts_dir)
    monkeypatch.setattr("src.core.site_onboarding.ONBOARDING_DIR", onboarding_dir)

    from src.core.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", data_dir)

    import src.web.sites_service as sites_mod

    sites_mod._service = None
    return {
        "site_id": site_id,
        "sites_path": sites_path,
        "schedule_path": schedule_path,
        "rules_dir": rules_dir,
        "scripts_dir": scripts_dir,
        "drafts_dir": drafts_dir,
        "onboarding_dir": onboarding_dir,
        "cron_path": data_dir / "script_cron_jobs.json",
    }


def test_delete_site_removes_config_files(delete_env):
    from src.core.site_config_write import delete_site
    from src.core.site_sync import load_sites_config

    site_id = delete_env["site_id"]
    result = delete_site(site_id)

    assert result["ok"] is True
    assert result["site_id"] == site_id
    assert result["data_preserved"] is True
    assert "sites.yaml" in result["deleted"]
    assert f"crawl_rules/{site_id}.yaml" in result["deleted"]
    assert "schedule.yaml" in result["deleted"]
    assert "script_cron_jobs.json" in result["deleted"]

    config = load_sites_config()
    assert all(s.get("id") != site_id for s in config.get("sites", []))
    assert not (delete_env["rules_dir"] / f"{site_id}.yaml").exists()
    assert not (delete_env["scripts_dir"] / f"{site_id}.json").exists()
    assert not (delete_env["drafts_dir"] / f"{site_id}.yaml").exists()
    assert not (delete_env["onboarding_dir"] / f"{site_id}.json").exists()

    schedule = yaml.safe_load(delete_env["schedule_path"].read_text(encoding="utf-8"))
    assert all(j.get("site_id") != site_id for j in schedule.get("jobs", []))
    assert any(j.get("site_id") == "other_site" for j in schedule.get("jobs", []))

    cron_data = json.loads(delete_env["cron_path"].read_text(encoding="utf-8"))
    cron_ids = {j["id"] for j in cron_data["jobs"]}
    assert "cron-demo" not in cron_ids
    assert "cron-other" in cron_ids


def test_delete_site_not_found(delete_env):
    from src.core.site_config_write import delete_site

    result = delete_site("no_such_site_xyz")
    assert result["ok"] is False
    assert "不存在" in result["error"]


def test_delete_protected_site(delete_env):
    from src.core.site_config_write import delete_site

    result = delete_site("protected_site")
    assert result["ok"] is False
    assert "受保护" in result["error"]


def test_api_delete_site(delete_env):
    from src.web.app import app

    site_id = delete_env["site_id"]
    client = TestClient(app)
    resp = client.delete(f"/api/sites/{site_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["site_id"] == site_id

    missing = client.delete("/api/sites/no_such_site_xyz")
    assert missing.status_code == 404

    protected = client.delete("/api/sites/protected_site")
    assert protected.status_code == 403


def test_sites_page_delete_script_after_appmodal():
    """删除逻辑必须在 AppModal 定义之后执行，否则 ReferenceError 导致按钮无响应。"""
    from src.web.app import app

    client = TestClient(app)
    html = client.get("/sites").text
    app_modal_pos = html.find("window.AppModal")
    init_pos = html.find("initDeleteSiteUI")
    bind_pos = html.find("AppModal.bind(deleteModal")
    assert app_modal_pos >= 0, "base.html 应定义 window.AppModal"
    assert init_pos >= 0, "sites.html 应包含 initDeleteSiteUI"
    assert bind_pos >= 0, "sites.html 应绑定删除确认 modal"
    assert app_modal_pos < init_pos, "AppModal 必须先于 initDeleteSiteUI 定义"
    assert app_modal_pos < bind_pos, "AppModal 必须先于 AppModal.bind 调用"
