"""TaskService 状态聚合测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.web.task_service import TaskService


SITE_ID = "中国能源建设集团有限公司_电子采购平台"


def _base_sync_row(**overrides):
    row = {
        "site_id": SITE_ID,
        "site_name": "电子采购平台",
        "status": "syncing",
        "notices_count": 0,
        "new_count": 0,
        "last_sync_at": None,
    }
    row.update(overrides)
    return row


def test_merge_generic_site_running_overrides_pending_run():
    """generic 站点不在 schedule jobs 中，运行中不应显示 pending。"""
    sync_svc = MagicMock()
    sync_svc.is_syncing.return_value = True
    sync_svc.get_sync_progress.return_value = {
        "run_id": "run_active_001",
        "is_running": True,
        "progress_items": 30,
        "max_items": 120,
        "current_step": "fetch_detail",
    }

    run_store = MagicMock()
    run_store.list_runs.return_value = []
    run_store.get_run.return_value = {
        "run_id": "run_active_001",
        "site_id": SITE_ID,
        "status": "running",
        "started_at": "2026-06-16T08:00:00+00:00",
        "notices_count": 30,
        "new_count": 0,
    }

    merged = TaskService._merge_site_row(
        _base_sync_row(),
        {},
        {"round_robin_interval_minutes": 10},
        sync_svc=sync_svc,
        run_store=run_store,
    )

    assert merged["is_running"] is True
    assert merged["last_run_status"] == "running"
    assert merged["last_run_status_label"] == "运行中"
    assert merged["active_run_id"] == "run_active_001"
    assert merged["display_status"] == "running"
    assert merged["sync_status_label"] == "同步中"
    assert merged["sync_progress"]["max_items"] == 120


def test_merge_idle_site_without_runs():
    sync_svc = MagicMock()
    sync_svc.is_syncing.return_value = False
    sync_svc.get_sync_progress.return_value = None

    run_store = MagicMock()
    run_store.list_runs.return_value = []

    merged = TaskService._merge_site_row(
        _base_sync_row(status="pending"),
        {},
        {"round_robin_interval_minutes": 10},
        sync_svc=sync_svc,
        run_store=run_store,
    )

    assert merged["is_running"] is False
    assert merged["last_run_status"] == "pending"
    assert merged["last_run_status_label"] == "待执行"
    assert merged["display_status"] == "pending"


def test_list_sites_counts_running_from_merged_rows():
    svc = TaskService()
    sync_data = {
        "backend": "json",
        "schedule": {},
        "summary": {"pending": 0, "syncing": 1, "completed": 0, "failed": 0},
        "sites": [_base_sync_row()],
    }
    sched_data = {
        "backend": "file",
        "schedule": {"round_robin_interval_minutes": 10},
        "summary": {"success": 0, "failed": 0, "running": 0},
        "jobs": [],
    }

    with patch("src.web.task_service.get_sync_service") as mock_sync, patch(
        "src.web.task_service.get_schedule_service"
    ) as mock_sched, patch("src.web.task_service.get_crawl_run_store") as mock_store, patch.object(
        TaskService, "_merge_site_row", wraps=TaskService._merge_site_row
    ) as mock_merge:
        mock_sync.return_value.list_status.return_value = sync_data
        mock_sched.return_value.list_jobs.return_value = sched_data
        mock_store.return_value.list_runs.return_value = []
        mock_store.return_value.get_run.return_value = {
            "run_id": "run_active_001",
            "status": "running",
            "started_at": "2026-06-16T08:00:00+00:00",
        }
        mock_sync.return_value.is_syncing.return_value = True
        mock_sync.return_value.get_sync_progress.return_value = {
            "run_id": "run_active_001",
            "is_running": True,
            "max_items": 120,
        }

        result = svc.list_sites()

    assert result["summary"]["run_running"] == 1
    assert mock_merge.called


def test_build_run_progress_uses_config_max_not_event_total():
    from datetime import datetime, timezone

    from src.web.sync_service import build_run_progress

    run_id = "run123"
    started = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    events = [
        {
            "type": "fetch_detail",
            "message": "抓取详情页 (30/109)",
            "timestamp": started.isoformat(),
        },
    ]

    with patch("src.web.sync_service.get_crawl_run_store") as mock_store, patch(
        "src.web.sync_service.get_site_by_id",
        return_value={"id": SITE_ID, "max_items": 120},
    ):
        store = mock_store.return_value
        store.get_run.return_value = {
            "run_id": run_id,
            "site_id": SITE_ID,
            "status": "running",
            "started_at": started.isoformat(),
        }
        store.list_events.return_value = events

        progress = build_run_progress(SITE_ID, run_id, started_at=started)

    assert progress["progress_items"] == 30
    assert progress["max_items"] == 120
