"""同步进度与中止逻辑测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.core.sync_cancel import SyncCancelledError, check_sync_cancelled, sync_cancel_scope
from src.web.sync_service import build_run_progress


@pytest.mark.asyncio
async def test_sync_cancel_scope():
    event = asyncio.Event()
    assert check_sync_cancelled() is False
    async with sync_cancel_scope(event):
        assert check_sync_cancelled() is False
        event.set()
        assert check_sync_cancelled() is True
    assert check_sync_cancelled() is False


def test_build_run_progress_from_fetch_detail_events():
    site_id = "test_site"
    run_id = "run123"
    started = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    run = {
        "run_id": run_id,
        "site_id": site_id,
        "status": "running",
        "started_at": started.isoformat(),
        "notices_count": 0,
    }
    events = [
        {"type": "start", "message": "开始", "timestamp": started.isoformat()},
        {
            "type": "fetch_detail",
            "message": "抓取详情页 (5/120)",
            "timestamp": started.isoformat(),
        },
    ]

    with patch("src.web.sync_service.get_crawl_run_store") as mock_store, patch(
        "src.web.sync_service.get_site_by_id",
        return_value={"id": site_id, "max_items": 120},
    ):
        store = mock_store.return_value
        store.get_run.return_value = run
        store.list_events.return_value = events

        progress = build_run_progress(site_id, run_id, started_at=started)

    assert progress["progress_items"] == 5
    assert progress["max_items"] == 120
    assert progress["current_step"] == "fetch_detail"
    assert progress["events_count"] == 2
    assert progress["latest_event_message"] == "抓取详情页 (5/120)"
    assert progress["progress_percent"] == pytest.approx(4.2, abs=0.1)


@pytest.mark.asyncio
async def test_cancel_sync_sets_event():
    from unittest.mock import MagicMock

    from src.web.sync_service import RunningSyncTask, SyncService, _running_tasks

    with patch("src.web.sync_service.get_sync_status_store", return_value=MagicMock()):
        svc = SyncService()
    event = asyncio.Event()
    _running_tasks["demo_site"] = RunningSyncTask(run_id="abc", cancel_event=event)

    result = await svc.cancel_sync("demo_site")
    assert result["ok"] is True
    assert event.is_set()

    _running_tasks.pop("demo_site", None)


@pytest.mark.asyncio
async def test_cancel_sync_when_not_running():
    from unittest.mock import MagicMock

    from src.web.sync_service import SyncService

    mock_store = MagicMock()
    mock_store.get_status.return_value = None
    with patch("src.web.sync_service.get_sync_status_store", return_value=mock_store), patch(
        "src.web.sync_service.get_crawl_run_store"
    ) as mock_run_store_fn:
        mock_run_store_fn.return_value.list_runs.return_value = []
        svc = SyncService()
        result = await svc.cancel_sync("nonexistent_site_xyz")
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_cancel_sync_cleans_stale_syncing_state():
    from unittest.mock import MagicMock

    from src.web.sync_service import SyncService

    mock_store = MagicMock()
    mock_store.get_status.return_value = "syncing"
    mock_store._get_doc.return_value = {
        "site_id": "stale_site",
        "status": "syncing",
        "last_sync_at": "2026-06-16T02:00:00+00:00",
        "notices_count": 5,
    }
    mock_run_store = MagicMock()
    mock_run_store.list_runs.return_value = [
        {"run_id": "run_stale", "status": "running", "started_at": "2026-06-16T02:00:00+00:00", "notices_count": 5}
    ]

    with patch("src.web.sync_service.get_sync_status_store", return_value=mock_store), patch(
        "src.web.sync_service.get_crawl_run_store", return_value=mock_run_store
    ):
        svc = SyncService()
        result = await svc.cancel_sync("stale_site")

    assert result["ok"] is True
    assert result["stale"] is True
    mock_run_store.finish_run.assert_called_once()
    mock_store.mark_cancelled.assert_called_once_with(
        "stale_site",
        error="用户中止（孤儿任务已清理）",
        duration_seconds=mock_store.mark_cancelled.call_args.kwargs["duration_seconds"],
        notices_count=5,
    )


def test_is_stale_sync_detects_orphan_state():
    from unittest.mock import MagicMock

    from src.web.sync_service import SyncService

    mock_store = MagicMock()
    mock_store.get_status.return_value = "syncing"
    mock_run_store = MagicMock()
    mock_run_store.list_runs.return_value = []

    with patch("src.web.sync_service.get_sync_status_store", return_value=mock_store), patch(
        "src.web.sync_service.get_crawl_run_store", return_value=mock_run_store
    ):
        svc = SyncService()
        assert svc.is_stale_sync("orphan_site") is True


def test_reset_sync_status_cleans_stale_state():
    from unittest.mock import MagicMock

    from src.web.sync_service import SyncService

    mock_store = MagicMock()
    mock_store.get_status.return_value = "syncing"
    mock_store._get_doc.return_value = {"site_id": "orphan_site", "status": "syncing", "last_sync_at": None}
    mock_run_store = MagicMock()
    mock_run_store.list_runs.return_value = []

    with patch("src.web.sync_service.get_sync_status_store", return_value=mock_store), patch(
        "src.web.sync_service.get_crawl_run_store", return_value=mock_run_store
    ), patch(
        "src.web.sync_service.get_site_by_id",
        return_value={"id": "orphan_site", "enabled": True},
    ):
        svc = SyncService()
        result = svc.reset_sync_status("orphan_site")

    assert result["ok"] is True
    mock_store.mark_cancelled.assert_called_once()


def test_get_sync_status_includes_stale_flag():
    from unittest.mock import MagicMock

    from src.web.sync_service import SyncService

    mock_store = MagicMock()
    mock_store.ensure_site.return_value = {"status": "syncing"}
    mock_store.get_status.return_value = "syncing"
    mock_run_store = MagicMock()
    mock_run_store.list_runs.return_value = []

    with patch("src.web.sync_service.get_sync_status_store", return_value=mock_store), patch(
        "src.web.sync_service.get_crawl_run_store", return_value=mock_run_store
    ), patch(
        "src.web.sync_service.get_site_by_id",
        return_value={"id": "orphan_site", "name": "测试站", "url": "https://example.com"},
    ):
        svc = SyncService()
        payload = svc.get_sync_status("orphan_site")

    assert payload["ok"] is True
    assert payload["stale"] is True


def test_sync_status_mark_cancelled():
    from src.core.sync_status import SyncStatusStore

    with patch.object(SyncStatusStore, "_connect_mongo"):
        store = SyncStatusStore()
    store._mongo_available = False
    store._memory = {}
    store.mark_cancelled("site_a", duration_seconds=12, notices_count=3)
    doc = store._get_doc("site_a")
    assert doc["status"] == "cancelled"
    assert doc["duration_seconds"] == 12
    assert doc["notices_count"] == 3


@pytest.mark.asyncio
async def test_trigger_sync_registers_run_before_return():
    from unittest.mock import AsyncMock, MagicMock

    from src.web.sync_service import SyncService, _running_tasks

    mock_run_store = MagicMock()
    with patch("src.web.sync_service.get_crawl_run_store", return_value=mock_run_store), patch(
        "src.web.sync_service.get_site_by_id",
        return_value={"id": "wechat_eabim", "name": "EaBIM"},
    ), patch(
        "src.web.sync_service.run_hermes_crawl_task",
        new_callable=AsyncMock,
        return_value={"success": True},
    ):
        svc = SyncService()
        svc.is_syncing = MagicMock(return_value=False)  # type: ignore[method-assign]
        result = await svc.trigger_sync("wechat_eabim")

    assert result["ok"] is True
    assert result["run_id"]
    mock_run_store.create_run.assert_called_once()
    call_kwargs = mock_run_store.create_run.call_args.kwargs
    assert call_kwargs["run_id"] == result["run_id"]
    assert call_kwargs["site_id"] == "wechat_eabim"
    assert call_kwargs["site_name"] == "EaBIM"
    assert call_kwargs["trigger"] == "manual"
    _running_tasks.pop("wechat_eabim", None)
