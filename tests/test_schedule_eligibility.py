"""Web 调度元信息与 list_site_jobs 对齐测试。"""

from __future__ import annotations

from unittest.mock import patch

from src.core.crawl_run_store import CrawlRunStore
from src.core.schedule_eligibility import filter_schedule_eligible_sites
from src.web.schedule_service import ScheduleService
from src.web.task_service import TaskService


def test_filter_respects_job_override_disabled():
    sites = [
        {"id": "a_site", "adapter": "ccgp", "enabled": True},
        {"id": "b_site", "adapter": "ccgp", "enabled": True},
    ]
    overrides = {"a_site": {"enabled": False}}
    result = filter_schedule_eligible_sites(sites, job_overrides=overrides)
    assert [s["id"] for s in result] == ["b_site"]


def test_list_site_jobs_includes_generic_with_crawl_rules():
    sites = [
        {
            "id": "中国电力建设集团有限公司_公共资源交易服务平台",
            "name": "电建平台",
            "adapter": "generic",
            "enabled": True,
        },
        {
            "id": "中国建筑集团有限公司_云筑网",
            "name": "云筑网",
            "adapter": "generic",
            "enabled": True,
        },
        {"id": "ccgp_national", "name": "政采网", "adapter": "ccgp", "enabled": True},
    ]
    schedule_meta = {"round_robin_interval_minutes": 10}

    with patch("src.core.crawl_run_store.get_settings") as mock_settings:
        mock_settings.return_value.mongodb_uri = None
        store = CrawlRunStore()
        with patch.object(store, "list_runs", return_value=[]):
            jobs = store.list_site_jobs(sites, schedule_meta)

    job_ids = [j["site_id"] for j in jobs]
    assert "中国电力建设集团有限公司_公共资源交易服务平台" in job_ids
    assert "中国建筑集团有限公司_云筑网" not in job_ids
    assert "ccgp_national" in job_ids

    power_job = next(
        j for j in jobs
        if j["site_id"] == "中国电力建设集团有限公司_公共资源交易服务平台"
    )
    assert power_job["schedule_index"] is not None
    assert power_job["stagger_offset_minutes"] is not None


def test_get_schedule_meta_counts_eligible_generic():
    with patch("src.core.crawl_run_store.get_settings") as mock_settings:
        mock_settings.return_value.mongodb_uri = None
        svc = ScheduleService()
        with patch(
            "src.web.schedule_service.load_enabled_sites",
            return_value=[
                {
                    "id": "中国电力建设集团有限公司_公共资源交易服务平台",
                    "adapter": "generic",
                    "enabled": True,
                },
                {
                    "id": "中国建筑集团有限公司_云筑网",
                    "adapter": "generic",
                    "enabled": True,
                },
                {"id": "ccgp_national", "adapter": "ccgp", "enabled": True},
            ],
        ):
            meta = svc.get_schedule_meta()

    assert meta["enabled_site_count"] == 2
    assert meta["per_site_cycle_minutes"] == (
        meta["enabled_site_count"] * meta["round_robin_interval_minutes"]
    )


def test_next_schedule_hint_for_eligible_generic_job():
    hint = TaskService._next_schedule_hint(
        {"schedule_index": 1, "stagger_offset_minutes": 10},
        {"round_robin_interval_minutes": 10},
    )
    assert hint == "队列 #1 · +10 min（每 10 min 1 站）"
