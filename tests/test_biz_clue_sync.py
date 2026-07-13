"""商机同步逻辑单元测试（mock）。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.biz_clue_config import clear_config_cache
from src.core.biz_clue_sync import load_biz_clue_sites, run_daily_biz_clue_sync


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_config_cache()
    yield
    clear_config_cache()


def test_load_biz_clue_sites_returns_enabled_sites():
    sites = load_biz_clue_sites()
    assert isinstance(sites, list)
    if sites:
        assert sites[0].get("id")
        assert sites[0].get("biz_clue_display_name")


def test_run_daily_biz_clue_sync_mock():
    mock_site = {
        "id": "ccgp_national",
        "name": "中国政府采购网",
        "adapter": "ccgp",
        "enabled": True,
        "biz_clue_display_name": "中国政府采购网",
        "biz_clue_level": "P0",
    }
    schedule_config = {
        "scheduler": {
            "daily_biz_clue_sync_stagger_seconds": 0,
            "daily_biz_clue_sync_max_items": 5,
            "daily_biz_clue_sync_analysis_enabled": False,
        }
    }

    async def _run():
        with patch("src.core.biz_clue_sync.load_biz_clue_sites", return_value=[mock_site]), patch(
            "src.core.biz_clue_sync.BrowserPool"
        ) as pool_cls, patch("src.core.biz_clue_sync.Pipeline") as pipeline_cls, patch(
            "src.core.biz_clue_sync.sync_site",
            new_callable=AsyncMock,
        ) as mock_sync:
            pool = pool_cls.return_value
            pool.start = AsyncMock()
            pool.stop = AsyncMock()
            mock_sync.return_value = {
                "success": True,
                "notices_count": 3,
                "new_count": 1,
                "biz_clue_count": 2,
                "duration_seconds": 1,
            }

            summary = await run_daily_biz_clue_sync(
                schedule_config=schedule_config,
                use_keyword_search=False,
                trigger_analysis=False,
            )

        assert summary["success"] is True
        assert summary["sites_ok"] == 1
        assert summary["total_notices"] == 3
        assert summary["total_biz_clue"] == 2
        pool.start.assert_awaited_once()
        pool.stop.assert_awaited_once()
        mock_sync.assert_awaited()
        pipeline_cls.assert_called_once()

    asyncio.run(_run())


def test_run_daily_biz_clue_sync_no_sites():
    schedule_config = {"scheduler": {}}

    async def _run():
        with patch("src.core.biz_clue_sync.load_biz_clue_sites", return_value=[]):
            return await run_daily_biz_clue_sync(schedule_config=schedule_config)

    summary = asyncio.run(_run())
    assert summary["success"] is False
    assert "没有可用的商机站点" in summary.get("error", "")
