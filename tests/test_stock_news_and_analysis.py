"""股票新闻采集与产业分析 API 测试。"""

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

from src.core.stock_news_fetcher import StockNewsFetcher, get_stock_news_fetcher
from src.core.stock_watch import get_industries, matched_industries
from src.web.stock_app import app as stock_app
from src.web.stock_insights_service import StockInsightsService
from src.web.data_service import NoticeDataService


@pytest.fixture
def stock_sample_notices(tmp_path: Path):
    now = datetime.now()
    docs = [
        {
            "title": "新能源储能项目设备采购及上市公司定增配套",
            "url": "https://example.com/stock-ne",
            "source_site_id": "crec_bidding",
            "source_site_name": "鲁班商务网",
            "category": "招标公告",
            "scraped_at": (now - timedelta(days=1)).isoformat(),
            "content_text": "涉及新能源储能产业链配套",
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
    ]
    jsonl_path = tmp_path / "notices.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for doc in docs:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
    return tmp_path


def test_get_industries_config():
    industries = get_industries()
    assert len(industries) >= 4
    names = {i["name"] for i in industries}
    assert "新能源" in names
    assert "半导体" in names


def test_matched_industries():
    inds = matched_industries({"title": "半导体设备国产化进展"})
    assert "半导体" in inds


def test_stock_news_fetch_mock(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("STOCK_NEWS_SOURCES", "mock")
    news_dir = tmp_path / "stock_news"
    monkeypatch.setattr("src.core.stock_news_fetcher.STOCK_NEWS_DIR", news_dir)
    monkeypatch.setattr("src.core.stock_news_fetcher.STOCK_NEWS_INDEX", news_dir / "index.json")
    import src.core.stock_news_fetcher as mod

    mod._fetcher = None
    fetcher = StockNewsFetcher()
    result = fetcher.fetch_all(days=7)
    assert result["ok"] is True
    assert result["deduped"] >= 1
    listed = fetcher.list_news(limit=10, days=7)
    assert listed["total"] >= 1
    assert listed["disclaimer"]
    mod._fetcher = None


def test_generate_industry_analysis_fallback(stock_sample_notices):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = stock_sample_notices
        settings.openai_api_key = None

        import src.web.stock_insights_service as stock_mod
        import src.web.data_service as data_mod

        data_mod._service = None
        stock_mod._service = None

        svc = StockInsightsService(NoticeDataService())
        result = svc.generate_industry_analysis(industry="新能源", days=7, use_llm=False)
        assert result["industry"] == "新能源"
        assert result["analysis"]["disclaimer"]
        assert "summary" in result["analysis"]

        data_mod._service = None
        stock_mod._service = None


def test_generate_investment_plan_fallback(stock_sample_notices):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = stock_sample_notices
        settings.openai_api_key = None

        import src.web.stock_insights_service as stock_mod
        import src.web.data_service as data_mod

        data_mod._service = None
        stock_mod._service = None

        svc = StockInsightsService(NoticeDataService())
        result = svc.generate_investment_plan(
            industries=["新能源"],
            risk_profile="balanced",
            horizon_months=12,
            use_llm=False,
        )
        assert result["horizon_months"] == 12
        assert result["plan"]["phases"]
        assert result["disclaimer"]

        data_mod._service = None
        stock_mod._service = None


def test_stock_analysis_api(stock_sample_notices, tmp_path: Path, monkeypatch):
    news_dir = tmp_path / "stock_news"
    monkeypatch.setattr("src.core.stock_news_fetcher.STOCK_NEWS_DIR", news_dir)
    monkeypatch.setattr("src.core.stock_news_fetcher.STOCK_NEWS_INDEX", news_dir / "index.json")
    monkeypatch.setenv("STOCK_NEWS_SOURCES", "mock")

    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = stock_sample_notices
        settings.openai_api_key = None

        import src.core.stock_news_fetcher as news_mod
        import src.web.stock_insights_service as stock_mod
        import src.web.data_service as data_mod

        news_mod._fetcher = None
        data_mod._service = None
        stock_mod._service = None

        get_stock_news_fetcher().fetch_all(days=7)

        client = TestClient(stock_app)
        resp = client.get("/api/stock/analysis?industry=新能源&use_llm=false")
        assert resp.status_code == 200
        body = resp.json()
        assert "industry_analysis" in body
        assert body["disclaimer"]

        plan_resp = client.post(
            "/api/stock/investment-plan",
            json={
                "industries": ["新能源"],
                "risk_profile": "balanced",
                "horizon_months": 12,
                "use_llm": False,
            },
        )
        assert plan_resp.status_code == 200
        plan_body = plan_resp.json()
        assert plan_body["ok"] is True
        assert plan_body["plan"]["markdown"]

        news_mod._fetcher = None
        data_mod._service = None
        stock_mod._service = None


def test_crawl_stock_analysis_tool(stock_sample_notices, tmp_path: Path, monkeypatch):
    from src.web.crawl_agent_tools import CrawlAgentToolExecutor

    news_dir = tmp_path / "stock_news"
    monkeypatch.setattr("src.core.stock_news_fetcher.STOCK_NEWS_DIR", news_dir)
    monkeypatch.setattr("src.core.stock_news_fetcher.STOCK_NEWS_INDEX", news_dir / "index.json")
    monkeypatch.setenv("STOCK_NEWS_SOURCES", "mock")

    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = stock_sample_notices
        settings.openai_api_key = None

        import src.core.stock_news_fetcher as news_mod
        import src.web.stock_insights_service as stock_mod
        import src.web.data_service as data_mod

        news_mod._fetcher = None
        data_mod._service = None
        stock_mod._service = None

        executor = CrawlAgentToolExecutor()
        result, summary = executor.execute(
            "crawl_stock_analysis",
            {"industry": "新能源", "days": 7, "use_llm": False},
        )
        assert result["ok"] is True
        assert "8092" in result["view_url"]
        assert "新能源" in summary

        plan_result, plan_summary = executor.execute(
            "crawl_stock_investment_plan",
            {"industries": ["新能源"], "horizon_months": 12, "use_llm": False},
        )
        assert plan_result["ok"] is True
        assert "12" in plan_summary

        news_mod._fetcher = None
        data_mod._service = None
        stock_mod._service = None
