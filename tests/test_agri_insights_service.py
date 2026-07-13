"""智慧农业洞察服务与 API 单元测试。"""

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

from src.web.app import app as main_app
from src.web.agri_insights_service import (
    AgriInsightsService,
    is_agri_related,
    matched_agri_keywords,
)
from src.web.data_service import NoticeDataService


@pytest.fixture
def agri_sample_notices(tmp_path: Path):
    now = datetime.now()
    docs = [
        {
            "title": "某市智慧农业物联网平台建设项目",
            "url": "https://example.com/agri1",
            "source_site_id": "crec_bidding",
            "source_site_name": "鲁班商务网",
            "category": "招标公告",
            "scraped_at": (now - timedelta(days=1)).isoformat(),
            "content_text": "采购人：某市农业农村局\n预算金额：380万元\n要求：农业物联网、传感器部署",
            "budget_amount": "380万元",
            "tender_party": "某市农业农村局",
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
            "title": "数字农业大数据平台运维服务",
            "url": "https://example.com/agri2",
            "source_site_id": "中国电力建设集团有限公司_公共资源交易服务平台",
            "source_site_name": "电建平台",
            "category": "招标公告",
            "scraped_at": now.isoformat(),
            "content_text": "数字农业、精准农业数据分析",
        },
    ]
    jsonl_path = tmp_path / "notices.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for doc in docs:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
    return jsonl_path


def test_is_agri_related_keywords():
    assert is_agri_related({"title": "智慧大棚控制系统采购"})
    assert not is_agri_related({"title": "普通办公用品采购"})
    kws = matched_agri_keywords({"title": "农业物联网监测项目"})
    assert "农业物联网" in kws


def test_agri_insights_summary_from_jsonl(agri_sample_notices):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = agri_sample_notices.parent

        svc = AgriInsightsService(NoticeDataService())
        summary = svc.get_summary(days=7)

    assert summary["agri_count_in_window"] == 2
    assert summary["counts"]["agri_true"] == 2
    assert len(summary["by_site"]) == 2
    assert summary["hot_keywords"][0]["keyword"] in ("智慧农业", "农业物联网", "数字农业")


def test_agri_insights_latest_from_jsonl(agri_sample_notices):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = agri_sample_notices.parent

        svc = AgriInsightsService(NoticeDataService())
        latest = svc.get_latest(limit=5, days=7)

    assert latest["total_in_window"] == 2
    assert len(latest["items"]) == 2
    assert all(i.get("agri_keywords") for i in latest["items"])


def test_agri_page_route(agri_sample_notices):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = agri_sample_notices.parent
        settings.openai_api_key = None

        import src.web.agri_insights_service as agri_mod
        import src.web.biz_clue_service as biz_mod
        import src.web.data_service as data_mod

        data_mod._service = None
        agri_mod._service = None
        biz_mod._service = None
        biz_mod._analysis_cache.clear()
        agri_mod._docs_cache.clear()
        agri_mod._summary_cache.clear()

        client = TestClient(main_app)
        resp = client.get("/insights")
        assert resp.status_code == 200
        assert "商机洞察" in resp.text
        assert "智能商机分析 Agent" in resp.text
        assert "LLM 增强分析" not in resp.text
        assert "智慧农业物联网平台" in resp.text
        assert "对话查询" not in resp.text
        assert "重新分析" not in resp.text
        assert "/insights/hermes" not in resp.text

        data_mod._service = None
        agri_mod._service = None
        biz_mod._service = None
        biz_mod._analysis_cache.clear()
        agri_mod._docs_cache.clear()
        agri_mod._summary_cache.clear()


def test_agri_api_insights(agri_sample_notices):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = agri_sample_notices.parent

        import src.web.agri_insights_service as agri_mod
        import src.web.data_service as data_mod

        data_mod._service = None
        agri_mod._service = None

        client = TestClient(main_app)
        resp = client.get("/api/agri/insights?days=7")
        assert resp.status_code == 200
        body = resp.json()
        assert body["agri_count_in_window"] == 2
        assert "structured" in body
        assert body["structured"]["total"] == 2

        data_mod._service = None
        agri_mod._service = None


def test_crawl_agri_insights_tool(agri_sample_notices):
    from src.web.crawl_agent_tools import CrawlAgentToolExecutor

    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = agri_sample_notices.parent

        import src.web.agri_insights_service as agri_mod
        import src.web.data_service as data_mod

        data_mod._service = None
        agri_mod._service = None

        executor = CrawlAgentToolExecutor()
        result, summary = executor.execute("crawl_agri_insights", {"days": 7})
        assert result["ok"] is True
        assert result["agri_count_in_window"] == 2
        assert "2 条" in summary
        assert "/insights" in result["view_url"]

        data_mod._service = None
        agri_mod._service = None


def test_agri_sync_api_starts_background(agri_sample_notices):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = agri_sample_notices.parent

        import src.web.app as app_mod

        app_mod._agri_sync_running = False
        app_mod._agri_last_sync_summary = None

        async def _noop(**kwargs):
            app_mod._agri_sync_running = False

        with patch.object(app_mod, "_run_agri_sync_job", side_effect=_noop):
            client = TestClient(main_app)
            resp = client.post("/api/agri/sync")
            assert resp.status_code == 200
            body = resp.json()
            assert body["ok"] is True
            assert body["running"] is True

        app_mod._agri_sync_running = False


def test_agri_classifier_keywords():
    from src.core.agri_classifier import classify_agri_by_keywords, score_agri_opportunity

    pos = classify_agri_by_keywords("智慧大棚控制系统采购", "含农业物联网传感器")
    assert pos.is_agri_related is True
    neg = classify_agri_by_keywords("普通办公用品采购", "桌椅板凳")
    assert neg.is_agri_related is False

    high = score_agri_opportunity(
        {
            "title": "某市智慧农业物联网平台建设项目",
            "category": "招标公告",
            "region": "广东省",
            "content_text": "预算金额：3800万元，农业物联网",
            "budget_amount": "3800万元",
        }
    )
    assert high["score"] >= 45
    assert high["tier"] in ("medium", "high")


def test_agri_tenders_api(agri_sample_notices):
    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = agri_sample_notices.parent

        import src.web.agri_insights_service as agri_mod
        import src.web.data_service as data_mod

        data_mod._service = None
        agri_mod._service = None

        client = TestClient(main_app)
        resp = client.get("/api/agri/tenders?days=7&min_score=0")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert len(body["items"]) == 2
        assert "opportunity_score" in body["items"][0]

        page = client.get("/insights/tenders?days=7")
        assert page.status_code == 200
        assert "招标商机" in page.text

        data_mod._service = None
        agri_mod._service = None


def test_crawl_agri_tenders_tool(agri_sample_notices):
    from src.web.crawl_agent_tools import CrawlAgentToolExecutor

    with patch("src.web.data_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.mongodb_uri = None
        settings.data_dir = agri_sample_notices.parent

        import src.web.agri_insights_service as agri_mod
        import src.web.data_service as data_mod

        data_mod._service = None
        agri_mod._service = None

        executor = CrawlAgentToolExecutor()
        result, summary = executor.execute("crawl_agri_tenders", {"days": 7, "limit": 5})
        assert result["ok"] is True
        assert result["total"] == 2
        assert "/insights" in result["view_url"]
        assert "商机" in summary

        data_mod._service = None
        agri_mod._service = None
