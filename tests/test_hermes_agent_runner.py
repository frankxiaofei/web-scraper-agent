"""Hermes Agent Runner 单元测试。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def enable_hermes_runner(monkeypatch):
    monkeypatch.setenv("CRAWL_VIA_HERMES", "true")
    monkeypatch.delenv("HERMES_AGENT_URL", raising=False)
    monkeypatch.setenv("WEB_SCRAPER_ROLE", "ui")


@pytest.fixture
def disable_hermes_runner(monkeypatch):
    monkeypatch.setenv("CRAWL_VIA_HERMES", "false")
    monkeypatch.delenv("HERMES_AGENT_URL", raising=False)


@pytest.fixture
def scheduler_gateway(monkeypatch):
    monkeypatch.setenv("CRAWL_VIA_HERMES", "true")
    monkeypatch.setenv("HERMES_AGENT_URL", "http://hermes-agent:8080")
    monkeypatch.setenv("WEB_SCRAPER_ROLE", "scheduler")


def test_crawl_via_hermes_enabled_default():
    from src.core.hermes_agent_runner import crawl_via_hermes_enabled

    assert crawl_via_hermes_enabled() is True


def test_crawl_via_hermes_disabled(monkeypatch):
    monkeypatch.setenv("CRAWL_VIA_HERMES", "false")
    from src.core.hermes_agent_runner import crawl_via_hermes_enabled

    assert crawl_via_hermes_enabled() is False


def test_should_use_hermes_gateway_scheduler_only(scheduler_gateway):
    from src.core.hermes_gateway_client import should_use_hermes_gateway

    assert should_use_hermes_gateway() is True


def test_should_not_use_gateway_on_app(enable_hermes_runner):
    from src.core.hermes_gateway_client import (
        should_execute_sync_directly,
        should_use_hermes_gateway,
    )

    assert should_use_hermes_gateway() is False
    assert should_execute_sync_directly() is True


@pytest.mark.asyncio
async def test_run_site_sync_calls_sync_and_audits(enable_hermes_runner, monkeypatch):
    from src.core.hermes_agent_runner import HermesTaskType, run_hermes_crawl_task

    mock_sync = AsyncMock(return_value={"site_id": "test_site", "success": True, "new_count": 1})
    mock_audit = MagicMock()
    monkeypatch.setattr(
        "src.core.hermes_agent_runner.get_site_by_id",
        lambda sid: {"id": sid, "enabled": True, "name": "Test", "url": "http://test"},
    )
    monkeypatch.setattr("src.core.hermes_agent_runner.load_sites_config", lambda: {"crawl_defaults": {}})
    monkeypatch.setattr("src.core.hermes_agent_runner.sync_site_by_id", mock_sync)
    monkeypatch.setattr("src.core.hermes_agent_runner.append_action", mock_audit)

    outcome = await run_hermes_crawl_task(
        HermesTaskType.SITE_SYNC,
        site_id="test_site",
        trigger="manual",
        run_id="run_test_1",
    )

    mock_sync.assert_awaited_once()
    assert outcome.get("success") is True
    assert outcome.get("hermes_runner") is True
    mock_audit.assert_called_once()
    assert mock_audit.call_args.kwargs.get("source") == "hermes"


@pytest.mark.asyncio
async def test_run_site_sync_via_gateway(scheduler_gateway, monkeypatch):
    from src.core.hermes_agent_runner import HermesTaskType, run_hermes_crawl_task

    mock_gateway = AsyncMock(
        return_value={"site_id": "test_site", "success": True, "hermes_gateway": True}
    )
    mock_sync = AsyncMock()
    mock_audit = MagicMock()
    monkeypatch.setattr(
        "src.core.hermes_agent_runner.get_hermes_gateway_client",
        lambda: MagicMock(run_site_sync=mock_gateway),
    )
    monkeypatch.setattr("src.core.hermes_agent_runner.sync_site_by_id", mock_sync)
    monkeypatch.setattr("src.core.hermes_agent_runner.append_action", mock_audit)

    outcome = await run_hermes_crawl_task(
        HermesTaskType.SITE_SYNC,
        site_id="test_site",
        trigger="scheduled",
    )

    mock_gateway.assert_awaited_once()
    mock_sync.assert_not_called()
    assert outcome.get("success") is True
    assert outcome.get("hermes_gateway") is True
    mock_audit.assert_called_once()


@pytest.mark.asyncio
async def test_run_site_sync_legacy_no_audit(disable_hermes_runner, monkeypatch):
    from src.core.hermes_agent_runner import HermesTaskType, run_hermes_crawl_task

    mock_sync = AsyncMock(return_value={"site_id": "test_site", "success": True})
    mock_audit = MagicMock()
    monkeypatch.setattr(
        "src.core.hermes_agent_runner.get_site_by_id",
        lambda sid: {"id": sid, "enabled": True, "name": "Test", "url": "http://test"},
    )
    monkeypatch.setattr("src.core.hermes_agent_runner.load_sites_config", lambda: {"crawl_defaults": {}})
    monkeypatch.setattr("src.core.hermes_agent_runner.sync_site_by_id", mock_sync)
    monkeypatch.setattr("src.core.hermes_agent_runner.append_action", mock_audit)

    outcome = await run_hermes_crawl_task(
        HermesTaskType.SITE_SYNC,
        site_id="test_site",
        trigger="scheduled",
    )

    assert outcome.get("success") is True
    assert "hermes_runner" not in outcome
    mock_audit.assert_not_called()


@pytest.mark.asyncio
async def test_run_daily_bim_sync_delegates(enable_hermes_runner, monkeypatch):
    from src.core.hermes_agent_runner import HermesTaskType, run_hermes_crawl_task

    mock_bim = AsyncMock(return_value={"success": True, "sites_ok": 2, "total_bim": 5})
    monkeypatch.setattr("src.core.daily_bim_sync.run_daily_bim_sync", mock_bim)
    monkeypatch.setattr(
        "src.core.hermes_agent_runner.append_action",
        lambda **kwargs: None,
    )

    summary = await run_hermes_crawl_task(
        HermesTaskType.DAILY_BIM_SYNC,
        trigger="daily_bim_sync",
    )

    mock_bim.assert_awaited_once()
    assert summary.get("hermes_runner") is True
    assert summary.get("sites_ok") == 2


@pytest.mark.asyncio
async def test_run_daily_bim_sync_via_gateway(scheduler_gateway, monkeypatch):
    from src.core.hermes_agent_runner import HermesTaskType, run_hermes_crawl_task

    mock_gateway = AsyncMock(return_value={"success": True, "sites_ok": 3, "hermes_gateway": True})
    mock_bim = AsyncMock()
    monkeypatch.setattr(
        "src.core.hermes_agent_runner.get_hermes_gateway_client",
        lambda: MagicMock(run_daily_bim_sync=mock_gateway),
    )
    monkeypatch.setattr("src.core.daily_bim_sync.run_daily_bim_sync", mock_bim)
    monkeypatch.setattr("src.core.hermes_agent_runner.append_action", lambda **kwargs: None)

    summary = await run_hermes_crawl_task(
        HermesTaskType.DAILY_BIM_SYNC,
        trigger="daily_bim_sync",
    )

    mock_gateway.assert_awaited_once()
    mock_bim.assert_not_called()
    assert summary.get("hermes_gateway") is True


@pytest.mark.asyncio
async def test_gateway_client_dispatch_retries(monkeypatch):
    from src.core.hermes_gateway_client import HermesGatewayClient

    monkeypatch.setenv("HERMES_AGENT_URL", "http://gateway.test:8080")
    calls = {"n": 0}

    class FakeResponse:
        status_code = 502

        @staticmethod
        def json():
            return {"ok": False, "error": "bad gateway"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            calls["n"] += 1
            if calls["n"] < 2:
                return FakeResponse()
            return type(
                "OkResp",
                (),
                {
                    "status_code": 200,
                    "json": staticmethod(
                        lambda: {
                            "ok": True,
                            "result": {"success": True, "data": {"site_id": "s1"}},
                        }
                    ),
                },
            )()

    monkeypatch.setattr("src.core.hermes_gateway_client.httpx.AsyncClient", FakeClient)
    client = HermesGatewayClient(retries=2)
    outcome = await client.dispatch(tool="crawl_trigger", arguments={"site_id": "s1"})
    assert outcome.get("success") is True
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_run_scheduler_site_sync_wrapper(enable_hermes_runner, monkeypatch):
    from src.core.hermes_agent_runner import run_scheduler_site_sync

    mock_task = AsyncMock(return_value={"site_id": "s1", "success": True})
    monkeypatch.setattr(
        "src.core.hermes_agent_runner.get_site_by_id",
        lambda sid: {"id": sid, "enabled": True, "adapter": "generic", "url": "http://s1"},
    )
    monkeypatch.setattr("src.core.hermes_agent_runner.load_sites_config", lambda: {"crawl_defaults": {}})
    monkeypatch.setattr("src.core.hermes_agent_runner.run_hermes_crawl_task", mock_task)

    outcome = await run_scheduler_site_sync("s1", trigger="scheduled")

    mock_task.assert_awaited_once()
    call_kwargs = mock_task.call_args.kwargs
    assert call_kwargs.get("trigger") == "scheduled"
    assert outcome.get("success") is True
