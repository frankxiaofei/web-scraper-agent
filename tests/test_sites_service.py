"""SitesService 与 /sites API 单元测试。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient


@pytest.fixture
def sites_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rules_dir = config_dir / "crawl_rules"
    rules_dir.mkdir()
    scripts_dir = data_dir / "crawl_scripts"
    scripts_dir.mkdir()

    sites_yaml = {
        "crawl_defaults": {"max_crawl_depth": 3},
        "sites": [
            {
                "id": "enabled_no_rule",
                "name": "启用无规则",
                "url": "https://enabled.example/",
                "adapter": "ccgp",
                "enabled": True,
            },
            {
                "id": "disabled_with_rule",
                "name": "禁用有规则",
                "url": "https://rule.example/",
                "adapter": "generic",
                "enabled": False,
            },
            {
                "id": "plain_disabled",
                "name": "纯禁用",
                "url": "https://plain.example/",
                "adapter": "generic",
                "enabled": False,
            },
        ],
    }
    (config_dir / "sites.yaml").write_text(
        yaml.dump(sites_yaml, allow_unicode=True), encoding="utf-8"
    )

    schedule_yaml = {
        "scheduler": {
            "per_site_schedule_enabled": True,
            "round_robin_interval_minutes": 12,
        },
        "jobs": [
            {
                "site_id": "disabled_with_rule",
                "trigger": "interval",
                "minutes": 45,
                "enabled": True,
            }
        ],
    }
    (config_dir / "schedule.yaml").write_text(
        yaml.dump(schedule_yaml, allow_unicode=True), encoding="utf-8"
    )

    (rules_dir / "disabled_with_rule.yaml").write_text(
        yaml.dump(
            {
                "site_id": "disabled_with_rule",
                "enabled": True,
                "list_page": {"strategy": "css", "item_selector": "a"},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    (scripts_dir / "enabled_no_rule.json").write_text(
        json.dumps(
            {
                "site_id": "enabled_no_rule",
                "script_type": "yaml_rule",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "script_path": "config/crawl_rules/enabled_no_rule.yaml",
                "valid": True,
            }
        ),
        encoding="utf-8",
    )

    script_cron_jobs = {
        "version": 1,
        "jobs": [
            {
                "id": "cron-1",
                "name": "定时单站",
                "script": "run_once.py",
                "args": ["--site-id", "enabled_no_rule"],
                "cron": "0 3 * * *",
                "enabled": True,
            }
        ],
    }
    (data_dir / "script_cron_jobs.json").write_text(
        json.dumps(script_cron_jobs), encoding="utf-8"
    )

    monkeypatch.setattr("src.core.site_sync.SITES_PATH", config_dir / "sites.yaml")
    monkeypatch.setattr("src.core.site_sync.SCHEDULE_PATH", config_dir / "schedule.yaml")
    monkeypatch.setattr("src.web.sites_service.SCHEDULE_PATH", config_dir / "schedule.yaml")
    monkeypatch.setattr("src.web.sites_service.CRAWL_SCRIPTS_DIR", scripts_dir)
    monkeypatch.setattr("src.core.crawl_rules.DEFAULT_RULES_DIR", rules_dir)
    monkeypatch.setattr("src.core.crawl_rules._default_loader", None)

    mock_store = MagicMock()
    mock_store.list_all.return_value = [
        {
            "site_id": "enabled_no_rule",
            "site_name": "启用无规则",
            "last_sync_at": "2026-06-01T10:00:00+00:00",
            "last_success_at": "2026-06-01T10:00:00+00:00",
            "status": "completed",
        }
    ]
    monkeypatch.setattr(
        "src.web.sites_service.get_sync_status_store",
        lambda: mock_store,
    )
    monkeypatch.setattr(
        "src.core.script_cron.JOBS_PATH",
        data_dir / "script_cron_jobs.json",
    )
    monkeypatch.setattr("src.core.script_cron.DATA_DIR", data_dir)

    import src.web.sites_service as sites_mod

    sites_mod._service = None
    return tmp_path


def test_extract_site_id_from_args():
    from src.web.sites_service import _extract_site_id_from_args

    assert _extract_site_id_from_args(["--site-id", "demo"]) == "demo"
    assert _extract_site_id_from_args(["--site_id", "x"]) == "x"
    assert _extract_site_id_from_args(["--other", "x"]) is None


def test_format_schedule_job_interval_and_cron():
    from src.web.sites_service import _format_schedule_job

    assert _format_schedule_job({"trigger": "interval", "minutes": 30}) == "每 30 分钟"
    assert _format_schedule_job({"trigger": "cron", "cron": "0 2 * * *"}) == "cron 0 2 * * *"


def test_list_sites_filters_unconfigured(sites_env):
    from src.web.sites_service import SitesService

    data = SitesService().list_sites()
    ids = {s["site_id"] for s in data["sites"]}

    assert "enabled_no_rule" in ids
    assert "disabled_with_rule" in ids
    assert "plain_disabled" not in ids
    assert data["summary"]["total"] == 2


def test_list_sites_merges_schedule_and_sync(sites_env):
    from src.web.sites_service import SitesService

    data = SitesService().list_sites()
    by_id = {s["site_id"]: s for s in data["sites"]}

    enabled = by_id["enabled_no_rule"]
    assert enabled["enabled"] is True
    assert enabled["crawl_method"] == "ccgp"
    assert "轮询 每 12 分钟/站" in enabled["schedule_hint"]
    assert "定时单站" in enabled["schedule_hint"]
    assert enabled["has_crawl_script"] is True
    assert enabled["last_sync_at"] == "2026-06-01T10:00:00+00:00"

    disabled = by_id["disabled_with_rule"]
    assert disabled["has_rule"] is True
    assert disabled["crawl_method"] == "规则"
    assert "每 45 分钟" in disabled["schedule_hint"]


def test_list_sites_include_all(sites_env):
    from src.web.sites_service import SitesService

    data = SitesService().list_sites(include_all=True)
    assert data["summary"]["total"] == 3


def test_api_sites_route(sites_env):
    from src.web.app import app

    client = TestClient(app)
    resp = client.get("/api/sites")
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["total"] == 2
    assert any(s["site_id"] == "enabled_no_rule" for s in body["sites"])


def test_sites_page_route(sites_env):
    from unittest.mock import MagicMock, patch

    from src.web.app import app

    mock_svc = MagicMock()
    mock_svc.data_source = "jsonl"
    with patch("src.web.app.get_data_service", return_value=mock_svc):
        client = TestClient(app)
        resp = client.get("/sites")
    assert resp.status_code == 200
    assert "站点列表" in resp.text
    assert "启用无规则" in resp.text
