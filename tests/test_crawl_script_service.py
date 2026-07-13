"""爬取脚本生成与执行（无 LLM 路径）单元测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.crawl_rules import CrawlRule, CrawlLimits, ListPage, Pagination
from src.core.crawl_script_service import (
    build_rule_summary,
    generate_crawl_script,
    get_crawl_script_status,
    load_crawl_script_metadata,
    run_generated_crawl,
    save_yaml_crawl_script,
)
from src.core.site_onboarding import get_onboarding_status, register_site
from src.web.crawl_agent_tools import CrawlAgentToolExecutor, DEFAULT_TOOL_SCHEMAS


FIXTURE_YAML = """\
version: 1
site_id: fixture_crawl_site
name: 测试站点
enabled: true
entry_url: https://example.com/list
list_page:
  strategy: dom
  container: table
  item: tr
  title: a
  link: a
pagination:
  type: next_button
  selector: .next
limits:
  max_pages: 2
  max_items: 10
"""


@pytest.fixture
def isolated_crawl_scripts(tmp_path, monkeypatch):
    metadata_dir = tmp_path / "crawl_scripts"
    rules_dir = tmp_path / "crawl_rules"
    drafts_dir = tmp_path / "sites_drafts"
    onboarding_dir = tmp_path / "site_onboarding"
    generated_dir = tmp_path / "generated"
    for d in (metadata_dir, rules_dir, drafts_dir, onboarding_dir, generated_dir):
        d.mkdir(parents=True)

    from src.core.crawl_rules import RuleLoader

    loader = RuleLoader(rules_dir=rules_dir)

    monkeypatch.setattr("src.core.crawl_script_service.METADATA_DIR", metadata_dir)
    monkeypatch.setattr("src.core.crawl_script_service.DRAFTS_DIR", drafts_dir)
    monkeypatch.setattr("src.core.crawl_script_service.GENERATED_PY_DIR", generated_dir)
    monkeypatch.setattr("src.core.crawl_rules.DEFAULT_RULES_DIR", rules_dir)
    monkeypatch.setattr("src.core.crawl_rules._default_loader", None)
    monkeypatch.setattr("src.core.crawl_rules.get_rule_loader", lambda: loader)
    monkeypatch.setattr("src.core.rule_generator.get_rule_loader", lambda: loader)
    monkeypatch.setattr("src.core.site_onboarding.DRAFTS_DIR", drafts_dir)
    monkeypatch.setattr("src.core.site_onboarding.ONBOARDING_DIR", onboarding_dir)
    return {
        "metadata_dir": metadata_dir,
        "rules_dir": rules_dir,
        "drafts_dir": drafts_dir,
        "generated_dir": generated_dir,
        "loader": loader,
    }


def test_build_rule_summary_from_preview():
    preview = {
        "site_id": "demo",
        "name": "演示",
        "entry_url": "https://demo.example/list",
        "list_page": {"strategy": "api"},
        "pagination": {"type": "ajax_param"},
        "detail": {"fetch_detail": True},
        "limits": {"max_items": 50, "max_pages": 5},
    }
    summary = build_rule_summary(preview)
    assert summary["list_strategy"] == "api"
    assert summary["pagination_type"] == "ajax_param"
    assert summary["fetch_detail"] is True
    assert summary["max_items"] == 50


def test_save_yaml_crawl_script_writes_rule_and_metadata(isolated_crawl_scripts):
    site_id = "fixture_crawl_site"
    isolated_crawl_scripts["drafts_dir"].joinpath(f"{site_id}.yaml").write_text(
        yaml.safe_dump({"id": site_id, "name": "测试", "url": "https://example.com"}),
        encoding="utf-8",
    )

    with patch("src.core.crawl_script_service.get_rule_loader", return_value=isolated_crawl_scripts["loader"]):
        result = save_yaml_crawl_script(site_id, FIXTURE_YAML, source_url="https://example.com/list")

    assert result["ok"] is True
    rule_path = isolated_crawl_scripts["rules_dir"] / f"{site_id}.yaml"
    assert rule_path.is_file()
    meta = load_crawl_script_metadata(site_id)
    assert meta is not None
    assert meta.script_type == "yaml_rule"
    assert meta.rule_summary.get("list_strategy") == "dom"


@pytest.mark.asyncio
async def test_generate_crawl_script_mock_llm(isolated_crawl_scripts):
    site_id = "gen_mock_site"
    isolated_crawl_scripts["drafts_dir"].joinpath(f"{site_id}.yaml").write_text(
        yaml.safe_dump({"id": site_id, "name": "Mock", "url": "https://mock.example"}),
        encoding="utf-8",
    )

    mock_gen = MagicMock()
    mock_gen.generate = AsyncMock(
        return_value={
            "yaml": FIXTURE_YAML.replace("fixture_crawl_site", site_id),
            "valid": True,
            "errors": [],
            "preview": yaml.safe_load(FIXTURE_YAML.replace("fixture_crawl_site", site_id)),
        }
    )

    with patch("src.core.crawl_script_service.get_rule_loader", return_value=isolated_crawl_scripts["loader"]):
        result = await generate_crawl_script(
            site_id=site_id,
            url="https://mock.example/list",
            save=True,
            generator=mock_gen,
        )

    assert result["ok"] is True
    assert result["saved"] is True
    assert load_crawl_script_metadata(site_id) is not None
    mock_gen.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_crawl_script_with_dry_run_mock(isolated_crawl_scripts):
    site_id = "dry_run_site"
    isolated_crawl_scripts["drafts_dir"].joinpath(f"{site_id}.yaml").write_text(
        yaml.safe_dump({"id": site_id, "name": "Dry", "url": "https://dry.example"}),
        encoding="utf-8",
    )
    yaml_text = FIXTURE_YAML.replace("fixture_crawl_site", site_id)

    mock_gen = MagicMock()
    mock_gen.generate = AsyncMock(
        return_value={
            "yaml": yaml_text,
            "valid": True,
            "errors": [],
            "preview": yaml.safe_load(yaml_text),
        }
    )

    dry_payload = {"success": True, "items": [{"title": "公告A", "url": "https://dry.example/1"}], "errors": []}

    with patch("src.core.crawl_script_service.get_rule_loader", return_value=isolated_crawl_scripts["loader"]), patch(
        "src.core.crawl_rule_dry_run.dry_run_crawl_rule", new=AsyncMock(return_value=dry_payload)
    ):
        result = await generate_crawl_script(
            site_id=site_id,
            dry_run=True,
            generator=mock_gen,
        )

    assert result["dry_run_ok"] is True
    assert result["dry_run"]["items"][0]["title"] == "公告A"


@pytest.mark.asyncio
async def test_run_generated_crawl_dry_run_no_llm(isolated_crawl_scripts):
    site_id = "exec_dry_site"
    site = {"id": site_id, "name": "执行测试", "url": "https://exec.example", "adapter": "generic"}
    isolated_crawl_scripts["drafts_dir"].joinpath(f"{site_id}.yaml").write_text(
        yaml.safe_dump(site),
        encoding="utf-8",
    )

    yaml_text = FIXTURE_YAML.replace("fixture_crawl_site", site_id)
    rules_dir = isolated_crawl_scripts["rules_dir"]
    rules_dir.joinpath(f"{site_id}.yaml").write_text(yaml_text, encoding="utf-8")

    meta_path = isolated_crawl_scripts["metadata_dir"] / f"{site_id}.json"
    meta_path.write_text(
        json.dumps(
            {
                "site_id": site_id,
                "version": 1,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "source_url": site["url"],
                "script_type": "yaml_rule",
                "script_path": f"config/crawl_rules/{site_id}.yaml",
                "valid": True,
                "rule_summary": {"list_strategy": "dom"},
            }
        ),
        encoding="utf-8",
    )

    dry_payload = {
        "success": True,
        "items": [{"title": "试跑条目", "url": "https://exec.example/n/1"}],
        "errors": [],
        "transport": "http",
    }

    with patch("src.core.crawl_rule_dry_run.dry_run_crawl_rule", new=AsyncMock(return_value=dry_payload)):
        result = await run_generated_crawl(site_id, dry_run=True)

    assert result["ok"] is True
    assert result["llm_used"] is False
    assert result["mode"] == "dry_run"
    assert len(result["items"]) == 1


@pytest.mark.asyncio
async def test_run_generated_crawl_sync_path_mock(isolated_crawl_scripts):
    site_id = "exec_sync_site"
    site = {"id": site_id, "name": "同步", "url": "https://sync.example", "adapter": "generic"}
    isolated_crawl_scripts["drafts_dir"].joinpath(f"{site_id}.yaml").write_text(
        yaml.safe_dump(site),
        encoding="utf-8",
    )
    rules_dir = isolated_crawl_scripts["rules_dir"]
    rules_dir.joinpath(f"{site_id}.yaml").write_text(
        FIXTURE_YAML.replace("fixture_crawl_site", site_id),
        encoding="utf-8",
    )

    meta_path = isolated_crawl_scripts["metadata_dir"] / f"{site_id}.json"
    meta_path.write_text(
        json.dumps(
            {
                "site_id": site_id,
                "version": 1,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "source_url": site["url"],
                "script_type": "yaml_rule",
                "script_path": f"config/crawl_rules/{site_id}.yaml",
                "valid": True,
                "rule_summary": {},
            }
        ),
        encoding="utf-8",
    )

    sync_result = {
        "site_id": site_id,
        "success": True,
        "notices_count": 3,
        "new_count": 2,
        "duration_seconds": 1,
        "run_id": "run_test",
    }

    with patch("src.core.crawl_script_service.sync_site", new=AsyncMock(return_value=sync_result)):
        result = await run_generated_crawl(site_id, persist=True, max_items=10)

    assert result["ok"] is True
    assert result["mode"] == "sync"
    assert result["notices_count"] == 3
    assert result["llm_used"] is False


def test_get_crawl_script_status(isolated_crawl_scripts):
    site_id = "status_site"
    rules_dir = isolated_crawl_scripts["rules_dir"]
    rules_dir.joinpath(f"{site_id}.yaml").write_text(
        FIXTURE_YAML.replace("fixture_crawl_site", site_id),
        encoding="utf-8",
    )
    isolated_crawl_scripts["drafts_dir"].joinpath(f"{site_id}.yaml").write_text(
        yaml.safe_dump({"id": site_id, "name": "状态", "url": "https://status.example"}),
        encoding="utf-8",
    )

    with patch("src.core.crawl_script_service.get_rule_loader", return_value=isolated_crawl_scripts["loader"]):
        status = get_crawl_script_status(site_id)

    assert status["ok"] is True
    assert status["has_rule_file"] is True
    assert status["rule_valid"] is True


def test_onboarding_advances_after_save(isolated_crawl_scripts):
    reg = register_site("Onboard 脚本站", "onboard-script.example.com")
    site_id = reg["site_id"]
    yaml_text = FIXTURE_YAML.replace("fixture_crawl_site", site_id)

    with patch("src.core.crawl_script_service.get_rule_loader", return_value=isolated_crawl_scripts["loader"]):
        save_yaml_crawl_script(site_id, yaml_text, source_url=reg["url"])

    from src.core.site_onboarding import mark_onboarding_stage_completed

    mark_onboarding_stage_completed(site_id, "analyze")
    mark_onboarding_stage_completed(site_id, "toolchain")
    status = get_onboarding_status(site_id)
    assert status is not None
    assert status.stages["analyze"] == "completed"
    assert status.stages["toolchain"] == "completed"


def test_hermes_tools_include_script_tools():
    names = {s["function"]["name"] for s in DEFAULT_TOOL_SCHEMAS}
    assert "crawl_generate_script" in names
    assert "crawl_run_script" in names
    assert "crawl_get_script_status" in names


def test_tool_crawl_run_script_dry_run_mock():
    executor = CrawlAgentToolExecutor()
    with patch(
        "src.core.crawl_script_service.run_generated_crawl",
        new=AsyncMock(
            return_value={
                "ok": True,
                "mode": "dry_run",
                "items": [{"title": "A"}],
                "llm_used": False,
            }
        ),
    ):
        result, summary = executor.execute("crawl_run_script", {"site_id": "demo", "dry_run": True})
    assert result["ok"] is True
    assert "试跑" in summary


def test_script_cron_whitelist_includes_run_generated():
    from src.core.script_cron import SCRIPT_WHITELIST

    assert "run_generated_crawl.py" in SCRIPT_WHITELIST
