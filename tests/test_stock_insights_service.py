"""股票领域洞察服务与 API 单元测试。"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web.stock_app import app as stock_app
from src.core.stock_watch import is_stock_related, matched_stock_keywords
from src.web.stock_insights_service import StockInsightsService
from src.web.data_service import NoticeDataService


@pytest.fixture
def stock_sample_notices(tmp_path: Path):
    now = datetime.now()
    docs = [
        {
            "title": "某上市公司定向增发项目咨询服务采购",
            "url": "https://example.com/stock1",
            "source_site_id": "crec_bidding",
            "source_site_name": "鲁班商务网",
            "category": "招标公告",
            "scraped_at": (now - timedelta(days=1)).isoformat(),
            "content_text": "涉及上市公司定增相关咨询服务",
        },
        {
            "title": "普通钢材采购",
            "url": "https://example.com/steel",
            "source_site_id": "crec_bidding",
            "source_site_name": "鲁班商务网",
            "category": "招标公告",
            "scraped_at": now.isoformat(),
            "content_text": "钢材采购",
        },
        {
            "title": "股权激励管理系统建设",
            "url": "https://example.com/stock2",
            "source_site_id": "dlzb_power",
            "source_site_name": "电力招标网",
            "category": "招标公告",
            "scraped_at": now.isoformat(),
            "content_text": "为上市公司搭建股权激励管理平台",
        },
    ]
    jsonl_path = tmp_path / "notices.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for doc in docs:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
    return jsonl_path


def test_is_stock_related_keywords():
    assert is_stock_related({"title": "上市公司定增公告"})
    assert not is_stock_related({"title": "普通办公用品采购"})
    kws = matched_stock_keywords({"title": "股权激励计划草案"})
    assert "股权激励" in kws


def test_stock_insights_summary_from_jsonl(stock_sample_notices):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = stock_sample_notices.parent

        svc = StockInsightsService(NoticeDataService())
        summary = svc.get_summary(days=7)

    assert summary["stock_count_in_window"] == 2
    assert summary["counts"]["stock_true"] == 2
    assert summary["data_strategy"] == "bid_notices_keyword_filter"
    assert len(summary["by_theme"]) >= 1


def test_stock_insights_mock_when_empty(tmp_path: Path):
    jsonl_path = tmp_path / "notices.jsonl"
    jsonl_path.write_text(
        json.dumps({"title": "普通采购", "content_text": "办公用品"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = tmp_path

        svc = StockInsightsService(NoticeDataService())
        summary = svc.get_summary(days=7)

    assert summary["data_strategy"] == "mock_demo"
    assert summary["stock_count_in_window"] >= 1
    assert summary["mock_count"] >= 1


def test_stock_insights_latest_from_jsonl(stock_sample_notices):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = stock_sample_notices.parent

        svc = StockInsightsService(NoticeDataService())
        latest = svc.get_latest(limit=5, days=7)

    assert latest["total_in_window"] == 2
    assert len(latest["items"]) == 2
    assert all(i.get("stock_keywords") for i in latest["items"])


def test_stock_page_route(stock_sample_notices):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = stock_sample_notices.parent
        settings.openai_api_key = None

        import src.web.stock_insights_service as stock_mod
        import src.web.data_service as data_mod

        data_mod._service = None
        stock_mod._service = None

        client = TestClient(stock_app)
        resp = client.get("/?use_llm=false")
        assert resp.status_code == 200
        assert "股票领域分析" in resp.text

        hermes = client.get("/hermes")
        assert hermes.status_code == 200
        assert "股票领域 Hermes" in hermes.text
        assert "hermes-theme-stock" in hermes.text

        data_mod._service = None
        stock_mod._service = None


def test_stock_api_insights(stock_sample_notices):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = stock_sample_notices.parent

        import src.web.stock_insights_service as stock_mod
        import src.web.data_service as data_mod

        data_mod._service = None
        stock_mod._service = None

        client = TestClient(stock_app)
        resp = client.get("/api/stock/insights?days=7")
        assert resp.status_code == 200
        body = resp.json()
        assert body["stock_count_in_window"] == 2
        assert "by_theme" in body

        data_mod._service = None
        stock_mod._service = None


def test_crawl_stock_insights_tool(stock_sample_notices):
    from src.web.crawl_agent_tools import CrawlAgentToolExecutor

    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = stock_sample_notices.parent

        import src.web.stock_insights_service as stock_mod
        import src.web.data_service as data_mod

        data_mod._service = None
        stock_mod._service = None

        executor = CrawlAgentToolExecutor()
        result, summary = executor.execute("crawl_stock_insights", {"days": 7})
        assert result["ok"] is True
        assert result["stock_count_in_window"] == 2
        assert "2 条" in summary
        assert "8092" in result["view_url"]

        data_mod._service = None
        stock_mod._service = None


def test_stock_sync_api_starts_background(stock_sample_notices):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = stock_sample_notices.parent

        import src.web.stock_app as stock_app_mod

        stock_app_mod._sync_running = False
        stock_app_mod._last_sync_summary = None

        async def _noop():
            stock_app_mod._sync_running = False

        with patch.object(stock_app_mod, "_run_stock_sync_job", side_effect=_noop):
            client = TestClient(stock_app)
            resp = client.post("/api/stock/sync")
            assert resp.status_code == 200
            body = resp.json()
            assert body["ok"] is True
            assert body["running"] is True

        stock_app_mod._sync_running = False
